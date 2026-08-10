"""uSMART 环境 profile —— 发现、脱敏摘要、fail-closed 激活。

━━━ 为什么会有这一层 ━━━

``UsmartGateway.default_setting`` 里每一项都是空串,所以「终端到底连的是哪一个
uSMART 主体」这件事完全由一个文件决定:``~/.vntrader/connect_usmart.json`` ——
vnpy 的 ConnectDialog 读它、写它,网关拿到的就是它。两套环境在被调通的过程中
各自留下了一份手写副本(``connect_usmart.uat.json`` /
``connect_usmart.real.json``)躺在它旁边。**那两份就是天然的 profile**,这个模块
只做一件事:把 ``connect_usmart.<名字>.json`` 认成一个具名环境,把「激活」定义
为一次原子的整文件替换。

不引入第五份配置格式、不把值搬进源码,是刻意的:凭据只存在于
``~/.vntrader/`` 下的那几个文件里,仓库里只有机制。**这个模块的任何返回值、
任何异常消息、任何 describe() 都不含 token / login_password / trade_password 的
内容**,`channel` 只露后 4 位、`phone` 只露末 2 位。

━━━ 为什么切换之前必须校验 ━━━

``vnpy_usmart.endpoints.endpoints()`` 对不认识的 region 退回 ``hk``、对不认识的
environment 退回 ``real``(见该函数 docstring)。这个 fallback 对网关是合理的
——运行时不该因为一个拼错的字段就崩;但对「切换环境」这个动作是致命的:把
``region`` 写成 ``hongkong`` 的一份 UAT profile 会被静默解析成 **hk/real**,也就是
生产,而界面上没有任何东西会变红。所以这里不复用 ``endpoints()`` 的宽容,而是
对着一份写死的合法组合表做成员判断,拼错即拒。

``hk`` + ``uat`` 是合法组合(endpoints 表里有这一行)却仍然被拒:2026-07 实测,
本机这套 channel 打 HK UAT 主机固定回 ``107012「非法OPEN请求」`` —— channel 是按
主体在各自主机上注册的,HK UAT 那台没有这条记录。让它连过去只会得到一个看起来
像凭据错误的报错,查一次要半小时。UAT 走 sg。

━━━ 备份即回退 ━━━

激活前先把当前生效那份原样(按字节,不重新序列化)复制成
``connect_usmart.previous.json``。而这个备份文件本身又符合 profile 命名,于是它
会作为一个名叫 ``previous`` 的 profile 出现在下拉里 —— 「切错了一步切回」不需要
第二套机制,就是再激活一次。连切两次 ``previous`` 会在两个环境之间来回摆,因为
激活的第一步是先把源文件读进内存,再做备份。

━━━ 一条诚实边界 ━━━

校验只看得见本地文件:region/environment 组合、RSA 私钥公钥能不能被解析、
area_code 与 phone 是否明显对不上。**它不发一个网络包**,所以「这套凭据在这台
主机上是否有效」它答不了 —— 那个答案只能由 ``login`` 给。这里挡掉的是那类
「连过去才发现是配置写错」的往返,不是凭据本身的正确性。

第二条边界更要紧,因为它跨过了这个模块的边界:**切换只改文件,不改 QuestDB**。
``run_gui.py:268`` 的开机自动连接读的是 ``gateway_config`` 存在 QuestDB 里的那份
setting,而不是 ``connect_usmart.json``。所以「切了 profile 但没点连接」这个动作
之后,下次开机自动连上的仍然是上一个环境。让这里去写 QuestDB 是错的解法 ——
那等于绕过 ConnectDialog 的 is_quote / is_trade / auto_connect 三个开关去改一行
交易配置。正解是切完就点一次「连接」,ConnectDialog 会把当前字段连同这三个
标志一起写回 QuestDB;GUI 侧因此在切换成功的日志里明写这一句。
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from vnpy.trader.utility import get_file_path

# ---------------------------------------------------------------------------
# 命名约定
# ---------------------------------------------------------------------------

#: vnpy 的 ConnectDialog 实际读写的文件名 —— 「当前生效」的定义就是它。
ACTIVE_FILENAME: str = "connect_usmart.json"

#: profile 文件名形状:PREFIX + 名字 + SUFFIX。注意 ACTIVE_FILENAME 不匹配这个
#: 形状(它少一个点),所以「当前生效」永远不会把自己列成一个可切换的 profile。
PROFILE_PREFIX: str = "connect_usmart."
PROFILE_SUFFIX: str = ".json"

#: 激活前备份用的 profile 名。它同时是回退入口 —— 见模块 docstring。
PREVIOUS_PROFILE: str = "previous"

#: 摘要里给「当前生效」这份用的显示名,不对应任何文件。
ACTIVE_LABEL: str = "当前生效"

#: ConnectDialog 用网关名判断要不要挂环境下拉。uSMART 网关注册名是大写。
USMART_GATEWAY_NAME: str = "USMART"

# ---------------------------------------------------------------------------
# 合法环境
# ---------------------------------------------------------------------------

#: 与 vnpy_usmart.endpoints._TABLE 的行对应。这里写死而不是去读那张私有表,
#: 是因为 endpoints() 对未知输入返回 fallback 而不抛错 —— 拿它做校验等于没校验
#: (见模块 docstring)。对应的回归手段是测试:每个组合必须解析出【互不相同】的
#: UsmartEndpoints 对象,多写一个上游没有的 region 会立刻和 hk 那行撞上而被抓到。
KNOWN_REGIONS: tuple[str, ...] = ("hk", "sg")
KNOWN_ENVIRONMENTS: tuple[str, ...] = ("real", "uat")

#: environment 取这个值即真金白银。GUI 靠它决定要不要把横幅刷成红色。
PRODUCTION_ENVIRONMENT: str = "real"

#: endpoints 表里有、但本机实测连不通的组合 —— 拒绝并说明理由,不静默放行。
REFUSED_COMBINATIONS: frozenset[tuple[str, str]] = frozenset({("hk", "uat")})

#: 只要出现在这个集合里的键,值一律不进任何字符串。redact() 用它。
SECRET_FIELDS: frozenset[str] = frozenset(
    {"token", "login_password", "trade_password"}
)

#: 大陆手机号形状:11 位、以 1 开头。香港号是 8 位。两者配错国别码是
#: area_code 这个字段最常见的用法错误(它是【手机号的】国别码,不是主体的)。
_MAINLAND_PHONE_LENGTH: int = 11
_HONGKONG_PHONE_LENGTH: int = 8
_MAINLAND_AREA_CODE: str = "86"
_HONGKONG_AREA_CODE: str = "852"


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class UsmartProfileError(Exception):
    """这个模块拒绝执行某件事时抛的基类。

    切换环境是一个「做一半比不做更糟」的动作:写坏了 connect_usmart.json,
    下一次连接会带着半份配置打到某个主机上。所以这里全部 fail-closed ——
    任何一步不确定就抛,不返回一个「大概成功了」的结果。
    """


class ProfileNotFoundError(UsmartProfileError):
    """点名的 profile 文件不存在,或存在但不是一个 JSON 对象。"""


class ProfileValidationError(UsmartProfileError):
    """校验没过。errors 是全部失败原因,不是第一条 —— 一次说清比来回试快。"""

    def __init__(self, name: str, errors: Iterable[str]) -> None:
        self.name = name
        self.errors: tuple[str, ...] = tuple(errors)
        joined = "; ".join(self.errors)
        super().__init__(f"profile {name!r} 校验未通过: {joined}")


class ProfileWriteError(UsmartProfileError):
    """写入或备份失败。抛出时保证 connect_usmart.json 仍是替换前那一份。"""


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------


def mask_phone(phone: str) -> str:
    """手机号打码:只保留末 2 位,其余按位数补星号。

    保留位数(而不是统一输出固定长度的星号)是有用的:area_code 那条弱一致性
    提醒要靠位数判断,操作者一眼也要能分清 8 位的港号和 11 位的陆号。
    """
    text = str(phone).strip()
    if len(text) <= 2:
        return "*" * len(text)
    return f"{'*' * (len(text) - 2)}{text[-2:]}"


def channel_tail(channel: str) -> str:
    """channel(對接編號)的后 4 位。不足 4 位就整体打星 —— 短到这个程度时
    「后 4 位」等于把它整个印出来。"""
    text = str(channel).strip()
    if len(text) < 4:
        return "*" * len(text) if text else "(未设置)"
    return f"…{text[-4:]}"


def redact(setting: Mapping[str, Any]) -> dict[str, str]:
    """把一份 setting 变成【可以放进日志和异常】的形状描述。

    秘密字段只报「已设置 / 空」,连长度都不给;phone 打码、channel 只留后 4 位;
    其余字段原样。这个函数是为了让「打印一下配置看看」这个再自然不过的调试动作
    不至于把凭据写进 run_gui.log。
    """
    shape: dict[str, str] = {}
    for key, value in setting.items():
        text = "" if value is None else str(value)
        if key in SECRET_FIELDS:
            shape[key] = "<已设置>" if text.strip() else "<空>"
        elif key == "phone":
            shape[key] = mask_phone(text)
        elif key == "channel":
            shape[key] = channel_tail(text)
        else:
            shape[key] = text
    return shape


# ---------------------------------------------------------------------------
# 摘要
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileSummary:
    """一份 profile 里【可以显示在屏幕上】的部分。

    刻意不持有原始 setting:这个对象会被塞进下拉框的 item text、写进日志、
    在异常里被格式化。它拿不到的东西就泄不出去。
    """

    name: str
    path: Path
    region: str
    environment: str
    markets: str
    area_code: str
    channel: str      # 已经是后 4 位形态,不是原值
    phone: str        # 已经打码,不是原值

    @property
    def is_production(self) -> bool:
        return self.environment == PRODUCTION_ENVIRONMENT

    def describe(self) -> str:
        """一行摘要,给下拉框和日志用。"""
        return (
            f"{self.name} — {self.region}/{self.environment} · "
            f"markets={self.markets} · channel {self.channel} · "
            f"phone +{self.area_code} {self.phone}"
        )

    def banner(self) -> str:
        """常驻横幅文案。「以为在 UAT 结果打到生产」是这个功能唯一能造成真金
        白银损失的方式,所以生产那一档要在文字上就自己说清楚,不能只靠颜色
        ——颜色在截图、在色盲、在被主题改过的样式表下都可能失效。"""
        tag = "【生产 REAL · 真实资金】" if self.is_production else "【UAT 模拟盘】"
        return (
            f"{tag} {self.region}/{self.environment} · markets={self.markets} · "
            f"channel {self.channel} · phone +{self.area_code} {self.phone}"
        )


def summarize(name: str, path: Path, setting: Mapping[str, Any]) -> ProfileSummary:
    """从一份 setting 里取出可安全展示的字段。缺字段按空串处理 —— 摘要的职责
    是「让人看出这是哪一套」,判断合不合法是 validate_setting 的事。"""
    return ProfileSummary(
        name=name,
        path=path,
        region=str(setting.get("region", "")).strip().lower(),
        environment=str(setting.get("environment", "")).strip().lower(),
        markets=str(setting.get("markets", "")).strip(),
        area_code=str(setting.get("area_code", "")).strip(),
        channel=channel_tail(str(setting.get("channel", ""))),
        phone=mask_phone(str(setting.get("phone", ""))),
    )


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationResult:
    """errors 非空即拒绝激活;warnings 只提示。

    两者分开是因为它们的错误代价不同:region 拼错会把单发到另一个主体去,
    而 area_code 与号码位数对不上只是「很可能填反了」—— 号段规则会变(内地
    早年也有非 1 开头的号段),把一条会过期的经验做成硬闸,代价是某天它会
    拦住一个完全正确的配置而没人知道该去哪里关掉它。
    """

    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def describe(self) -> str:
        parts: list[str] = []
        if self.errors:
            parts.append("拒绝: " + "; ".join(self.errors))
        if self.warnings:
            parts.append("提醒: " + "; ".join(self.warnings))
        return " | ".join(parts) if parts else "校验通过"


def _pem_problem(path_text: str, label: str, *, private: bool) -> str | None:
    """PEM 文件能不能用。能用返回 None,不能用返回给人看的中文原因。

    用 cryptography 直接 load 而不是 shell 出去调 openssl:这里要判断的是
    「网关待会儿加载它时会不会抛」,而网关走的就是
    ``serialization.load_pem_{private,public}_key``(见 rest_client.py:282 /
    trade_client.py:182)。换一个解析器就等于换了一个判据 —— openssl 认得的
    格式 cryptography 未必认(反之亦然),那样这道闸会在两个方向上都说谎。
    """
    file = Path(path_text).expanduser()
    if not file.is_file():
        return f"{label} 指向的文件不存在: {file}"

    try:
        payload = file.read_bytes()
        if private:
            key = serialization.load_pem_private_key(payload, password=None)
            if not isinstance(key, RSAPrivateKey):
                return f"{label} 不是 RSA 私钥: {file}"
        else:
            pub = serialization.load_pem_public_key(payload)
            if not isinstance(pub, RSAPublicKey):
                return f"{label} 不是 RSA 公钥: {file}"
    except Exception as exc:  # noqa: BLE001 — cryptography 的解析失败类型随后端版本变化,这里只需要「解析不了」这一个事实
        return f"{label} 无法解析({type(exc).__name__}: {exc}): {file}"

    return None


def validate_setting(setting: Mapping[str, Any]) -> ValidationResult:
    """激活前的全部本地检查。返回而不抛 —— 调用方(GUI)要能一次显示所有问题。"""
    errors: list[str] = []
    warnings: list[str] = []

    region = str(setting.get("region", "")).strip().lower()
    environment = str(setting.get("environment", "")).strip().lower()

    if region not in KNOWN_REGIONS:
        errors.append(
            f"region={region!r} 不是已知主体(可选 {'/'.join(KNOWN_REGIONS)});"
            "endpoints() 对未知 region 会静默退回 hk,也就是把 UAT 配置打到生产主机"
        )
    if environment not in KNOWN_ENVIRONMENTS:
        errors.append(
            f"environment={environment!r} 不是已知环境"
            f"(可选 {'/'.join(KNOWN_ENVIRONMENTS)});endpoints() 对未知值会静默退回 real"
        )

    if (region, environment) in REFUSED_COMBINATIONS:
        errors.append(
            f"拒绝 {region}/{environment}:本机实测这套 channel 打 HK UAT 主机固定回"
            "107012「非法OPEN请求」(channel 未在该主机注册)。UAT 请用 region=sg"
        )

    key_path = str(setting.get("rsa_key_path", "")).strip()
    if not key_path:
        errors.append("rsa_key_path 为空:签名必需 RSA 私钥 PEM,网关连不上")
    else:
        problem = _pem_problem(key_path, "rsa_key_path(签名私钥)", private=True)
        if problem:
            errors.append(problem)

    # 公钥是【交易】开关的一半(另一半是 trade_password):为空是一个合法的
    # 只读配置,不是错误。但它为空而使用者以为自己切到了能下单的环境,是一次
    # 静默的功能缺失 —— 所以提醒。
    pub_key_path = str(setting.get("rsa_pub_key_path", "")).strip()
    if not pub_key_path:
        warnings.append("rsa_pub_key_path 为空:该 profile 只能行情只读,发不出单")
    else:
        problem = _pem_problem(pub_key_path, "rsa_pub_key_path(加密公钥)", private=False)
        if problem:
            errors.append(problem)

    warnings.extend(_area_code_warnings(setting))

    return ValidationResult(tuple(errors), tuple(warnings))


def _area_code_warnings(setting: Mapping[str, Any]) -> list[str]:
    """area_code 与 phone 的弱一致性。只警告 —— 见 ValidationResult 的 docstring。

    这个字段坑在语义上而不在取值上:它是【登录手机号的】国别码,不是账户主体
    的。一个香港生产账户用大陆号码登录,这里就必须填 86 —— 而 API 把填错报成
    一个普通的凭据失败(见 vnpy_usmart/endpoints.py 模块 docstring 记的那次)。
    """
    area_code = str(setting.get("area_code", "")).strip()
    phone = str(setting.get("phone", "")).strip()
    if not area_code or not phone:
        return []

    masked = mask_phone(phone)
    if (
        len(phone) == _MAINLAND_PHONE_LENGTH
        and phone.startswith("1")
        and area_code == _HONGKONG_AREA_CODE
    ):
        return [
            f"area_code={area_code} 配 {len(phone)} 位、1 开头的号码({masked})几乎肯定填反了:"
            f"area_code 是【手机号的】国别码,大陆号应为 {_MAINLAND_AREA_CODE}"
        ]
    if len(phone) == _HONGKONG_PHONE_LENGTH and area_code == _MAINLAND_AREA_CODE:
        return [
            f"area_code={area_code} 配 {len(phone)} 位号码({masked})可能填反了:"
            f"香港号是 8 位,应为 {_HONGKONG_AREA_CODE}"
        ]
    return []


# ---------------------------------------------------------------------------
# 路径与发现
# ---------------------------------------------------------------------------


def default_profile_dir() -> Path:
    """profile 所在目录,即 vnpy 的 ``~/.vntrader``。

    从 get_file_path 反推而不是自己拼 ``Path.home() / ".vntrader"``:vnpy 的
    TRADER_DIR 会随进程启动目录变化(见 vnpy/trader/utility.py 的
    _get_trader_dir),自己拼出来的那份在某些启动方式下根本不是 ConnectDialog
    读的那个目录。
    """
    return get_file_path(ACTIVE_FILENAME).parent


def _resolve_dir(directory: Path | None) -> Path:
    return default_profile_dir() if directory is None else directory


def profile_path(name: str, directory: Path | None = None) -> Path:
    return _resolve_dir(directory) / f"{PROFILE_PREFIX}{name}{PROFILE_SUFFIX}"


def active_path(directory: Path | None = None) -> Path:
    return _resolve_dir(directory) / ACTIVE_FILENAME


def _profile_name(path: Path) -> str:
    return path.name[len(PROFILE_PREFIX) : -len(PROFILE_SUFFIX)]


def _read_setting(path: Path) -> dict[str, Any]:
    """读一份 profile。不是 JSON 对象就当它不存在 —— 一个内容是列表或者被截断
    的文件,和一个缺失的文件,对「能不能切过去」这件事是同一个答案。"""
    try:
        payload = path.read_text(encoding="UTF-8")
    except OSError as exc:
        raise ProfileNotFoundError(f"读取 {path.name} 失败: {type(exc).__name__}: {exc}") from exc

    try:
        loaded: Any = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProfileNotFoundError(f"{path.name} 不是合法 JSON: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ProfileNotFoundError(f"{path.name} 顶层不是 JSON 对象,不能当 profile 用")
    return loaded


def discover_profiles(directory: Path | None = None) -> list[ProfileSummary]:
    """扫出全部可用 profile 的脱敏摘要。

    读不出来的文件被跳过而不是报错:下拉框列不出一个坏文件是可以接受的,
    而在别处存在一个坏文件就打不开连接对话框不行。真去激活它时
    ``activate_profile`` 会原地抛出确切原因。

    排序:名字字典序,``previous`` 永远排最后 —— 它是回退口,不是一个环境,
    放在末尾能少一次误点。
    """
    folder = _resolve_dir(directory)
    if not folder.is_dir():
        return []

    summaries: list[ProfileSummary] = []
    for path in sorted(folder.glob(f"{PROFILE_PREFIX}*{PROFILE_SUFFIX}")):
        if path.name == ACTIVE_FILENAME:
            continue
        try:
            setting = _read_setting(path)
        except ProfileNotFoundError:
            continue
        summaries.append(summarize(_profile_name(path), path, setting))

    summaries.sort(key=lambda item: (item.name == PREVIOUS_PROFILE, item.name))
    return summaries


def active_summary(directory: Path | None = None) -> ProfileSummary | None:
    """当前生效那份的摘要;文件不存在或读不出来返回 None。

    这里吞掉读失败(而 activate_profile 不吞)是因为调用方是常驻横幅:一个坏
    掉的 connect_usmart.json 应该让横幅显示「未知」,不该让主界面起不来。
    """
    path = active_path(directory)
    try:
        setting = _read_setting(path)
    except ProfileNotFoundError:
        return None
    return summarize(ACTIVE_LABEL, path, setting)


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def _write_atomic_bytes(path: Path, payload: bytes) -> None:
    """同目录临时文件 → fsync → os.replace。权限 0600。

    同目录是必须的:os.replace 只在同一文件系统上原子,``/tmp`` 与
    ``~/.vntrader`` 在本机是两个卷。fsync 也是必须的:没有它,替换本身原子,
    但断电后可能留下一个长度正确、内容全零的 connect_usmart.json。
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            # mkstemp 本身就用 0600 建文件;这里再显式一次,是把「这份文件里
            # 有凭据」写成代码而不是写成注释 —— 将来谁把 mkstemp 换成别的
            # 建法,这一行仍然成立。
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise ProfileWriteError(f"写入 {path.name} 失败: {type(exc).__name__}: {exc}") from exc


def harden_permissions(path: Path) -> None:
    """把一份凭据文件收紧到 0600。

    vnpy 的 ``save_json`` 用 ``open(mode="w+")`` 建文件,权限由 umask 决定
    (本机 022 → 0644,同机其他用户可读)。ConnectDialog 每次点「连接」都会走
    那条路径重写 connect_usmart.json,所以每次都要收一次;失败不抛 —— 权限
    收不紧是个安全退化,但让它挡住一次连接是更大的损失。
    """
    # 文件系统不支持 chmod(exFAT / 某些网络盘)时无声退化 —— 这里没有可做的
    # 补救,报错也只会变成一句使用者无法处理的噪音。
    with suppress(OSError):
        path.chmod(0o600)


# ---------------------------------------------------------------------------
# 激活
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActivationResult:
    """激活成功后的回执。backup 为 None 表示激活前没有「当前生效」可备份。"""

    summary: ProfileSummary
    warnings: tuple[str, ...]
    backup: Path | None

    def describe(self) -> str:
        parts = [self.summary.banner()]
        if self.backup is not None:
            parts.append(f"原配置已备份为 {self.backup.name}(可选 {PREVIOUS_PROFILE} 切回)")
        if self.warnings:
            parts.append("提醒: " + "; ".join(self.warnings))
        return " | ".join(parts)


def activate_profile(name: str, directory: Path | None = None) -> ActivationResult:
    """把 ``name`` 这份 profile 变成当前生效的 ``connect_usmart.json``。

    四步顺序是有讲究的:①先把源读进内存 ②校验 ③备份当前生效 ④原子替换。
    ①在②之前,是为了让「激活 previous」变成一次干净的对调 —— 备份那一步会
    覆盖 previous 文件,而那时源内容已经在内存里了。③在④之前,是因为④一旦
    成功就再也读不到旧配置。②在③之前,是因为一份连校验都过不了的 profile
    不配让当前生效那份被动一下。
    """
    source = profile_path(name, directory)
    if not source.is_file():
        raise ProfileNotFoundError(f"没有名为 {name!r} 的 profile: {source}")

    setting = _read_setting(source)

    result = validate_setting(setting)
    if not result.ok:
        raise ProfileValidationError(name, result.errors)

    active = active_path(directory)
    backup: Path | None = None
    if active.is_file():
        backup = profile_path(PREVIOUS_PROFILE, directory)
        # 按【字节】复制,不重新序列化:当前生效那份如果已经被手工改坏了
        # (半个 JSON、非法编码),重新序列化会先抛,于是连备份都做不成 ——
        # 而那正是最需要能回退的时候。
        try:
            current_bytes = active.read_bytes()
        except OSError as exc:
            raise ProfileWriteError(
                f"读取当前配置以备份失败,已放弃切换: {type(exc).__name__}: {exc}"
            ) from exc
        _write_atomic_bytes(backup, current_bytes)

    payload = json.dumps(setting, indent=4, ensure_ascii=False) + "\n"
    _write_atomic_bytes(active, payload.encode("UTF-8"))

    return ActivationResult(
        summary=summarize(name, source, setting),
        warnings=result.warnings,
        backup=backup,
    )

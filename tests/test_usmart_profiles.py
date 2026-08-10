"""uSMART 环境 profile 的发现、脱敏、校验与激活。

全部在 tmp_path 里跑:被测的是「把一份 JSON 变成当前生效配置」这套动作,而它
在真目录里跑一次就会改掉 ~/.vntrader/connect_usmart.json —— 也就是下一次真连
接会用的那份。所有 profile 都由本文件现造,凭据一律是占位值
(phone="00000000" / channel="000..." / 密码字面量 "PLACEHOLDER-*"),RSA 密钥
每次现场生成,仓库里不留任何真实材料。

一条本文件自己守着的纪律:任何一处断言都不许把占位密钥、token 或密码写进
「期望输出」—— 那样写出来的用例在别人拿真配置跑时会把真凭据打进 pytest 的
diff 里。检查脱敏的方式统一是「断言这个子串不出现」。
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vnpy_usmart.endpoints import endpoints  # noqa: E402

from fluent_ui.usmart_profiles import (  # noqa: E402
    ACTIVE_FILENAME,
    KNOWN_ENVIRONMENTS,
    KNOWN_REGIONS,
    PREVIOUS_PROFILE,
    ProfileNotFoundError,
    ProfileValidationError,
    activate_profile,
    active_summary,
    channel_tail,
    discover_profiles,
    mask_phone,
    profile_path,
    redact,
    validate_setting,
)

# 占位凭据。刻意长得不像真值,且在断言里只当「不应出现的子串」用。
PLACEHOLDER_TOKEN = "PLACEHOLDER-TOKEN-NOT-A-REAL-JWT"
PLACEHOLDER_LOGIN_PASSWORD = "PLACEHOLDER-LOGIN-PW"
PLACEHOLDER_TRADE_PASSWORD = "PLACEHOLDER-TRADE-PW"
# 全用 0 / 9 这种重复位,不用顺子:顺手敲出来的连号往往正好是某个真 UAT 口令的
# 前缀,一次泄漏扫描的假阳性会让下一个人以为这份测试真抄了什么进来。
PLACEHOLDER_CHANNEL = "0000009999"
PLACEHOLDER_HK_PHONE = "00000000"
PLACEHOLDER_CN_PHONE = "10000000000"

SECRET_LITERALS = (
    PLACEHOLDER_TOKEN,
    PLACEHOLDER_LOGIN_PASSWORD,
    PLACEHOLDER_TRADE_PASSWORD,
)


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


def write_keypair(folder: Path, stem: str) -> tuple[Path, Path]:
    """现场生成一对 RSA 密钥并落盘,返回 (私钥, 公钥) 路径。

    1024 位而不是 2048:这里检验的是「PEM 能不能被 cryptography 解析出正确的
    类型」,与密钥强度无关,而生成耗时是平方级的 —— 2048 位在本机实测每对约
    120ms,乘上用到密钥的用例数就是一秒多的固定开销。
    """
    private = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    key_file = folder / f"{stem}.pem"
    pub_file = folder / f"{stem}.pub.pem"

    key_file.write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub_file.write_bytes(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return key_file, pub_file


def make_setting(
    key_file: Path,
    pub_file: Path,
    *,
    region: str = "sg",
    environment: str = "uat",
    area_code: str = "86",
    phone: str = PLACEHOLDER_CN_PHONE,
    markets: str = "hk",
) -> dict[str, Any]:
    return {
        "token": PLACEHOLDER_TOKEN,
        "phone": phone,
        "login_password": PLACEHOLDER_LOGIN_PASSWORD,
        "area_code": area_code,
        "rsa_key_path": str(key_file),
        "channel": PLACEHOLDER_CHANNEL,
        "region": region,
        "environment": environment,
        "markets": markets,
        "orderbook_push": "no",
        "tick_push": "no",
        "trade_password": PLACEHOLDER_TRADE_PASSWORD,
        "rsa_pub_key_path": str(pub_file),
    }


def write_profile(folder: Path, name: str, setting: dict[str, Any]) -> Path:
    path = profile_path(name, folder)
    path.write_text(json.dumps(setting, indent=4, ensure_ascii=False), encoding="UTF-8")
    return path


@pytest.fixture
def keys(tmp_path: Path) -> tuple[Path, Path]:
    return write_keypair(tmp_path, "placeholder")


@pytest.fixture
def workspace(tmp_path: Path, keys: tuple[Path, Path]) -> Path:
    """一个装着 uat / real 两份 profile 和一份当前生效配置的假 ~/.vntrader。"""
    key_file, pub_file = keys
    folder = tmp_path / "vntrader"
    folder.mkdir()

    write_profile(folder, "uat", make_setting(key_file, pub_file, region="sg", environment="uat"))
    write_profile(
        folder,
        "real",
        make_setting(key_file, pub_file, region="hk", environment="real", markets="hk,us"),
    )
    (folder / ACTIVE_FILENAME).write_text(
        json.dumps(make_setting(key_file, pub_file, region="sg", environment="uat")),
        encoding="UTF-8",
    )
    return folder


# ---------------------------------------------------------------------------
# 合法组合表与 endpoints() 保持一致
# ---------------------------------------------------------------------------


def test_every_declared_combination_resolves_to_a_distinct_endpoint_row() -> None:
    """这条是本模块那张写死的组合表的唯一回归手段。

    endpoints() 对未知输入返回 fallback 而不抛错,所以「多列了一个上游没有的
    region」不会以任何形式报错 —— 它只会悄悄解析成 hk 那一行。用【互不相同】
    来抓:多写一个不存在的 region,它必然和 hk 的两行之一撞上。
    """
    resolved = [
        endpoints(region, environment)
        for region in KNOWN_REGIONS
        for environment in KNOWN_ENVIRONMENTS
    ]
    assert len({id(item) for item in resolved}) == len(KNOWN_REGIONS) * len(KNOWN_ENVIRONMENTS)


# ---------------------------------------------------------------------------
# 发现与脱敏
# ---------------------------------------------------------------------------


def test_discover_lists_named_profiles_and_never_the_active_file(workspace: Path) -> None:
    names = [summary.name for summary in discover_profiles(workspace)]
    assert names == ["real", "uat"]


def test_discover_puts_the_backup_profile_last(workspace: Path, keys: tuple[Path, Path]) -> None:
    key_file, pub_file = keys
    write_profile(workspace, PREVIOUS_PROFILE, make_setting(key_file, pub_file))

    names = [summary.name for summary in discover_profiles(workspace)]
    assert names == ["real", "uat", PREVIOUS_PROFILE]


def test_discover_skips_an_unparseable_file_instead_of_failing(workspace: Path) -> None:
    profile_path("broken", workspace).write_text("{not json", encoding="UTF-8")

    names = [summary.name for summary in discover_profiles(workspace)]
    assert names == ["real", "uat"]


def test_discover_on_a_missing_directory_returns_empty(tmp_path: Path) -> None:
    assert discover_profiles(tmp_path / "nope") == []


def test_summary_never_carries_any_secret_field(workspace: Path) -> None:
    rendered = " ".join(
        summary.describe() + summary.banner() + repr(summary)
        for summary in discover_profiles(workspace)
    )
    for secret in SECRET_LITERALS:
        assert secret not in rendered


def test_summary_shows_only_the_last_four_channel_digits(workspace: Path) -> None:
    summary = next(item for item in discover_profiles(workspace) if item.name == "uat")

    assert summary.channel.endswith(PLACEHOLDER_CHANNEL[-4:])
    assert PLACEHOLDER_CHANNEL not in summary.describe()


def test_summary_masks_the_phone_down_to_two_digits(workspace: Path) -> None:
    summary = next(item for item in discover_profiles(workspace) if item.name == "uat")

    assert summary.phone == "*" * (len(PLACEHOLDER_CN_PHONE) - 2) + PLACEHOLDER_CN_PHONE[-2:]
    assert PLACEHOLDER_CN_PHONE not in summary.describe()


def test_production_profile_says_so_in_words_not_only_in_colour(workspace: Path) -> None:
    real = next(item for item in discover_profiles(workspace) if item.name == "real")
    uat = next(item for item in discover_profiles(workspace) if item.name == "uat")

    assert real.is_production
    assert "生产" in real.banner()
    assert not uat.is_production
    assert "UAT" in uat.banner()


def test_mask_phone_and_channel_tail_survive_short_inputs() -> None:
    assert mask_phone("") == ""
    assert mask_phone("12") == "**"
    assert mask_phone("909") == "*09"
    assert channel_tail("") == "(未设置)"
    assert channel_tail("12") == "**"


def test_redact_reports_secrets_as_presence_only(keys: tuple[Path, Path]) -> None:
    key_file, pub_file = keys
    shape = redact(make_setting(key_file, pub_file))

    assert shape["token"] == "<已设置>"
    assert shape["login_password"] == "<已设置>"
    assert shape["trade_password"] == "<已设置>"
    assert shape["region"] == "sg"
    for secret in SECRET_LITERALS:
        assert secret not in json.dumps(shape, ensure_ascii=False)


def test_redact_distinguishes_an_empty_secret_from_a_set_one(keys: tuple[Path, Path]) -> None:
    key_file, pub_file = keys
    setting = make_setting(key_file, pub_file)
    setting["token"] = ""

    assert redact(setting)["token"] == "<空>"


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def test_a_well_formed_sg_uat_profile_passes(keys: tuple[Path, Path]) -> None:
    key_file, pub_file = keys
    result = validate_setting(make_setting(key_file, pub_file))

    assert result.ok
    assert result.warnings == ()


def test_unknown_region_is_refused_because_endpoints_would_silently_fall_back(
    keys: tuple[Path, Path],
) -> None:
    key_file, pub_file = keys
    result = validate_setting(make_setting(key_file, pub_file, region="hongkong"))

    assert not result.ok
    assert any("region" in message for message in result.errors)


def test_unknown_environment_is_refused(keys: tuple[Path, Path]) -> None:
    key_file, pub_file = keys
    result = validate_setting(make_setting(key_file, pub_file, environment="sandbox"))

    assert not result.ok
    assert any("environment" in message for message in result.errors)


def test_hk_uat_is_refused_with_the_measured_error_code(keys: tuple[Path, Path]) -> None:
    """hk/uat 在 endpoints 表里【是】一行合法配置 —— 拒绝它靠的是实测,不是
    表结构,所以理由必须带上 107012,否则下一个人只会以为是这里写漏了。"""
    key_file, pub_file = keys
    result = validate_setting(make_setting(key_file, pub_file, region="hk", environment="uat"))

    assert not result.ok
    assert any("107012" in message for message in result.errors)
    assert any("sg" in message for message in result.errors)


def test_hk_real_and_sg_uat_are_both_allowed(keys: tuple[Path, Path]) -> None:
    key_file, pub_file = keys

    assert validate_setting(make_setting(key_file, pub_file, region="hk", environment="real")).ok
    assert validate_setting(make_setting(key_file, pub_file, region="sg", environment="uat")).ok


def test_a_missing_private_key_file_is_refused(keys: tuple[Path, Path], tmp_path: Path) -> None:
    _, pub_file = keys
    setting = make_setting(tmp_path / "absent.pem", pub_file)
    result = validate_setting(setting)

    assert not result.ok
    assert any("rsa_key_path" in message for message in result.errors)


def test_an_empty_private_key_path_is_refused(keys: tuple[Path, Path]) -> None:
    key_file, pub_file = keys
    setting = make_setting(key_file, pub_file)
    setting["rsa_key_path"] = ""

    assert not validate_setting(setting).ok


def test_a_pem_that_openssl_cannot_parse_is_refused(
    keys: tuple[Path, Path], tmp_path: Path
) -> None:
    """一个存在但内容是垃圾的文件,是这道闸和「文件存在吗」那道闸的分界。

    PEM 头拼出来而不是写成整段字面量,是 test_no_tracked_credentials.py 点名
    要求的写法:那道闸对 PEM 头【不设任何豁免通道】,因为唯一可能的豁免判据是
    「所在文件看着像测试」,而测试文件恰恰是最常见的泄漏点。代价就落在这种
    「需要一个假 PEM 头」的用例上,出路是这一行而不是豁免名单。
    """
    _, pub_file = keys
    junk = tmp_path / "junk.pem"
    header = "-----" + "BEGIN RSA PRIVATE KEY" + "-----"
    junk.write_text(f"{header}\nnot base64\n", encoding="UTF-8")

    result = validate_setting(make_setting(junk, pub_file))

    assert not result.ok
    assert any("无法解析" in message for message in result.errors)


def test_a_public_key_offered_as_the_signing_key_is_refused(keys: tuple[Path, Path]) -> None:
    """两条路径都指同一个公钥,是最容易手抖抖出来的一种:文件存在、是合法
    PEM、连扩展名都对。load_pem_private_key 在这里会直接抛,所以判据只是
    「rsa_key_path 这一项没过」,不预设它以哪种理由没过。"""
    _, pub_file = keys
    result = validate_setting(make_setting(pub_file, pub_file))

    assert not result.ok
    assert any("rsa_key_path" in message for message in result.errors)


def test_a_non_rsa_private_key_is_refused_by_type_not_by_parse(tmp_path: Path) -> None:
    """Ed25519 的 PEM 解析得动 —— 只有 isinstance 那一步拦得住它,而网关正是
    在同一步抛(rest_client.py:282)。这条用例守的就是那一步没被省掉。"""
    private = ed25519.Ed25519PrivateKey.generate()
    key_file = tmp_path / "ed25519.pem"
    key_file.write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub_file = tmp_path / "ed25519.pub.pem"
    pub_file.write_bytes(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    result = validate_setting(make_setting(key_file, pub_file))

    assert any("不是 RSA 私钥" in message for message in result.errors)
    assert any("不是 RSA 公钥" in message for message in result.errors)


def test_a_blank_public_key_path_only_warns(keys: tuple[Path, Path]) -> None:
    key_file, pub_file = keys
    setting = make_setting(key_file, pub_file)
    setting["rsa_pub_key_path"] = ""
    result = validate_setting(setting)

    assert result.ok
    assert any("只读" in message for message in result.warnings)


def test_mainland_number_with_hongkong_area_code_warns_but_does_not_refuse(
    keys: tuple[Path, Path],
) -> None:
    key_file, pub_file = keys
    result = validate_setting(
        make_setting(key_file, pub_file, area_code="852", phone=PLACEHOLDER_CN_PHONE)
    )

    assert result.ok
    assert any("area_code" in message for message in result.warnings)
    assert PLACEHOLDER_CN_PHONE not in result.describe()


def test_hongkong_number_with_mainland_area_code_warns(keys: tuple[Path, Path]) -> None:
    key_file, pub_file = keys
    result = validate_setting(
        make_setting(key_file, pub_file, area_code="86", phone=PLACEHOLDER_HK_PHONE)
    )

    assert result.ok
    assert result.warnings


def test_a_hongkong_number_with_its_own_area_code_is_quiet(keys: tuple[Path, Path]) -> None:
    key_file, pub_file = keys
    result = validate_setting(
        make_setting(key_file, pub_file, area_code="852", phone=PLACEHOLDER_HK_PHONE)
    )

    assert result.ok
    assert result.warnings == ()


# ---------------------------------------------------------------------------
# 激活
# ---------------------------------------------------------------------------


def test_activation_replaces_the_active_file_with_the_profile(workspace: Path) -> None:
    result = activate_profile("real", workspace)

    active = json.loads((workspace / ACTIVE_FILENAME).read_text(encoding="UTF-8"))
    assert active["region"] == "hk"
    assert active["environment"] == "real"
    assert result.summary.is_production


def test_activation_leaves_the_active_file_owner_only(workspace: Path) -> None:
    """0600 不是洁癖:这份文件里是登录密码与交易密码,而同机其他账号默认读得到
    一个 0644 的文件。"""
    activate_profile("real", workspace)

    mode = stat.S_IMODE((workspace / ACTIVE_FILENAME).stat().st_mode)
    assert mode == 0o600


def test_activation_leaves_no_temporary_file_behind(workspace: Path) -> None:
    activate_profile("real", workspace)

    assert [path.name for path in workspace.glob("*.tmp")] == []


def test_activation_backs_up_the_previous_active_config(workspace: Path) -> None:
    before = (workspace / ACTIVE_FILENAME).read_bytes()

    result = activate_profile("real", workspace)

    assert result.backup == profile_path(PREVIOUS_PROFILE, workspace)
    assert result.backup is not None
    assert result.backup.read_bytes() == before


def test_activating_the_backup_swaps_the_two_environments_back(workspace: Path) -> None:
    """「切错了一步切回」不靠第二套机制:备份文件本身就是一个 profile。

    顺序上这只有在【先把源读进内存、再做备份】时才成立 —— 反过来的话备份会
    先覆盖 previous,再去读它,读到的是刚被覆盖的当前配置,一切回去就是原地
    不动。这条用例锁的就是那个顺序。
    """
    activate_profile("real", workspace)
    assert json.loads((workspace / ACTIVE_FILENAME).read_text(encoding="UTF-8"))["region"] == "hk"

    activate_profile(PREVIOUS_PROFILE, workspace)

    active = json.loads((workspace / ACTIVE_FILENAME).read_text(encoding="UTF-8"))
    assert active["region"] == "sg"
    assert active["environment"] == "uat"


def test_activation_without_an_existing_active_file_reports_no_backup(
    workspace: Path,
) -> None:
    (workspace / ACTIVE_FILENAME).unlink()

    result = activate_profile("real", workspace)

    assert result.backup is None
    assert (workspace / ACTIVE_FILENAME).is_file()


def test_a_corrupt_active_file_is_still_backed_up_byte_for_byte(workspace: Path) -> None:
    """最需要能回退的时刻,恰好是当前配置已经被手工改坏的时刻 —— 所以备份走
    字节复制而不是重新序列化。"""
    (workspace / ACTIVE_FILENAME).write_text("{half a json", encoding="UTF-8")

    result = activate_profile("real", workspace)

    assert result.backup is not None
    assert result.backup.read_text(encoding="UTF-8") == "{half a json"


def test_a_refused_profile_leaves_the_active_config_untouched(
    workspace: Path, keys: tuple[Path, Path]
) -> None:
    key_file, pub_file = keys
    write_profile(
        workspace,
        "trap",
        make_setting(key_file, pub_file, region="hk", environment="uat"),
    )
    before = (workspace / ACTIVE_FILENAME).read_bytes()

    with pytest.raises(ProfileValidationError, match="107012"):
        activate_profile("trap", workspace)

    assert (workspace / ACTIVE_FILENAME).read_bytes() == before
    assert not profile_path(PREVIOUS_PROFILE, workspace).exists()


def test_activating_a_name_that_has_no_file_raises(workspace: Path) -> None:
    with pytest.raises(ProfileNotFoundError, match="absent"):
        activate_profile("absent", workspace)


def test_activating_a_profile_whose_json_is_broken_raises(workspace: Path) -> None:
    profile_path("broken", workspace).write_text("[]", encoding="UTF-8")

    with pytest.raises(ProfileNotFoundError, match="JSON 对象"):
        activate_profile("broken", workspace)


def test_validation_errors_never_echo_a_secret(
    workspace: Path, keys: tuple[Path, Path]
) -> None:
    key_file, pub_file = keys
    write_profile(
        workspace,
        "trap",
        make_setting(key_file, pub_file, region="atlantis", environment="uat"),
    )

    with pytest.raises(ProfileValidationError) as excinfo:
        activate_profile("trap", workspace)

    for secret in SECRET_LITERALS:
        assert secret not in str(excinfo.value)


def test_active_summary_reads_the_current_file(workspace: Path) -> None:
    summary = active_summary(workspace)

    assert summary is not None
    assert (summary.region, summary.environment) == ("sg", "uat")

    activate_profile("real", workspace)
    switched = active_summary(workspace)

    assert switched is not None
    assert switched.is_production


def test_active_summary_returns_none_rather_than_raising_on_a_broken_file(
    workspace: Path,
) -> None:
    """常驻横幅的调用方 —— 一个坏掉的配置文件应该让横幅显示「未知」,不该让
    主界面起不来。"""
    (workspace / ACTIVE_FILENAME).write_text("{oops", encoding="UTF-8")

    assert active_summary(workspace) is None

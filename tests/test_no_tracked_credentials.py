"""真实凭据不许进 git —— 对各仓【已跟踪文件】做形状扫描。

`.gitignore` 只挡「未跟踪」。它挡不住 `git add -f`，挡不住有人把 RSA 私钥
粘进 `.py`，也挡不住 `~/.vntrader/connect_usmart.json` 被复制进仓后改个名。
本轮补的那七行 ignore 规则解决的是「手滑 `git add -A`」，而这条用例解决的是
另一半：**已经在 git 索引里的内容**。两者缺一都不够。

为什么必须是「已跟踪」而不是「工作树全部文件」：`.venv/`、`lab/`、
`questdb-data/` 这些目录里合法地躺着大量高熵二进制，扫它们只会得到一屏
噪声，噪声多到一定程度这条用例就会被人删掉——这是它最可能的死法，
所以入口选 `git ls-files`。

━━━ 四条形状 ━━━

* **PEM 私钥头。** 出现在纯文本里几乎不可能是别的东西，因此【不设任何
  豁免通道】：即使是一次性生成的测试密钥也要拒。正确写法是像
  `vnpy_usmart/tests/conftest.py` 那样在 fixture 里现场 `generate_private_key()`，
  跑完即弃、一个字节都不落盘。

  「不设豁免」是有代价的，代价落在**需要一个假 PEM 头**的用例上——比如
  「给一个只有头、没有体的垃圾文件，解析必须失败」这种。那类用例照样会被判红，
  它们的出路不是加豁免名单，而是像下面 `PEM_SAMPLE` 那样把头拼出来
  （`"-----" + "BEGIN …" + "-----"`），一行的事。之所以宁可付这个代价：能豁免
  PEM 的判据只有「所在文件/行看着像测试」，而测试文件恰恰是最常见的泄漏点，
  那个豁免等于把最强的一条形状变成最弱的一条。
* **≥200 字符的连续 base64。** 1024 位 RSA 私钥的 base64 体大约 850 字符，
  即便剥掉 PEM 头也躲不过这一条。阈值取 200 是量出来的：9 个自建仓的 303 个
  已跟踪文件里，最长的一段 base64 字符类连续串是 **84 字符**（一条测试函数名，
  `test_a_semantics_bump_that_tou...`），200 留了一倍以上余量。
* **`Bearer <token>`。** 根目录 `.mcp.json` 里就明文放着一个，那份文件的约定是
  「不上 GitHub 即可」，但同样形状的东西一旦被抄进任何一个仓就是事故。
* **手机号 + 键名。** 11 位大陆号或 8 位港号，且【同一行上】另有 `phone` /
  `mobile` / `tel` / `手机` 这类键名后跟 `:` 或 `=`。不要求键名就等于把每个
  8 位整数都报一遍（日期、成交量、价格），一次实测里连 `vendor.css` 都被
  `tel` 命中过。

━━━ 怎么把真凭据和占位凭据分开 ━━━

这是这条用例能不能活下来的关键。本项目的测试里到处是 `"TOKEN123"`
`"qwe123456"` `phone="00000000"` 这类占位值，全报一遍的下场就是被删。判据分三层，
任何一层认定是占位就放行：

1. **标记词**（`example` / `dummy` / `fake` / `placeholder` / `test` …）。
   只查【命中片段自身】，不查整行——查整行的话，一把真钥匙只要落在
   `test_*.py` 里就会被那个 `test` 洗白，而测试文件恰恰是最常见的泄漏点。
2. **香农熵**，只对 base64 与 Bearer 生效，下限 3.5 bit/char。真凭据实测在
   5.3~6.0 之间（探针在 notebook 的 PNG 输出上量到 5.30~6.00），
   而 `"A"*250` 是 0.0、`"abc123"` 循环是 2.58。
3. **数字形态**，只对手机号生效：全同、只有两种数字、末尾 ≥4 位重复、
   整串等差（`12345678`），外加一张已知假号白名单（`13800138000` 等）。

**它会漏什么**（写在这里是为了让人别把这条用例当成保险）：

* 短凭据没有形状。32 字符的 API token、密码、`channel`、对接编号——正则看不见
  它们与普通标识符的区别。**这条用例给不了「仓里没有凭据」这个结论**，
  它只给「没有这四种形状的凭据」。
* 只看工作树里的已跟踪内容，**不看 git 历史**。历史里的真凭据由
  `vnpy_usmart/docs/CREDENTIAL_HISTORY.md` 挂账，那份文件明写「AI 不执行
  history rewrite / force push」，只能由仓库所有者手动跑 filter-repo。
* 二进制文件（解不出 UTF-8 的）整份跳过。塞进 `.png` 的私钥抓不到。
* 手机号那条只看单行。参数分行写的 `login(\n  "1xxxxxxxxxx",\n)` 抓不到。
* 巧合落进占位形态的真号（例如尾号 `0000` 的真实手机号）会被放行。

━━━ `~/.vntrader` 与仓库路径的关系 ━━━

实测：`~/.vntrader` 解析为 `/Users/flink/.vntrader`，而 11 个仓全部在
`/Volumes/ORICO/Developer/vnpy-workspace/` 下——**不同的卷，谈不上包含关系**，
任何 `git add` 都够不到它。下面 `test_the_vntrader_home_is_outside_every_repo`
把这件事钉成断言而不是留成印象：真正的凭据只该住在那里，哪天有人为了省事
把 `TRADER_DIR` 指进仓内，这条会先红。
"""

from __future__ import annotations

import math
import os
import random
import re
import string
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 仓库发现
# ---------------------------------------------------------------------------

APP_ROOT = Path(__file__).resolve().parents[1]

# 9 个自建仓。两个 fork（vnpy / vnpy_ctastrategy）不在扫描集里：它们的
# examples/ 下有十几个 notebook，输出单元里嵌着上百万字符的 PNG base64，
# 探针在那两个仓量到 40+ 条 ≥200 的命中且熵全在 5.5 以上——为它们建豁免名单，
# 等于给「长 base64」这条形状开一个按路径生效的后门，而后门迟早会被扩大。
# 代价是诚实的：fork 里我们自己新增的文件不受这条用例保护，只受 .gitignore 保护。
SELF_BUILT_REPOS: tuple[str, ...] = (
    "vnpy_app",
    "vnpy_alphakit",
    "vnpy_gatewaykit",
    "vnpy_futu",
    "vnpy_usmart",
    "vnpy_recorder",
    "vnpy_replay",
    "vnpy_router",
    "vnpy_agentbridge",
)

# 兄弟仓在本机是平铺同级目录，在 CI 里被 actions/checkout 解到 vnpy_app/_deps/
# （见 .github/workflows/ci.yml 的六个 "Checkout ..." 步骤）。两种布局都认，
# 认不到的仓静默跳过——CI 只解出其中 5 个，要求全部到齐会让这条用例在 CI 上
# 永远红着，而一条永远红的用例等于没有。
CANDIDATE_PARENTS: tuple[Path, ...] = (APP_ROOT.parent, APP_ROOT / "_deps")

# 单文件上限。9 个仓最大的已跟踪文件远在这之下；设上限只是为了让这条用例
# 在有人日后往仓里放大文件时仍然是秒级的。超限文件会被计入 skipped 并在
# 断言消息里点名，不会被静默忽略。
MAX_BYTES: int = 2 * 1024 * 1024


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def discover_repos() -> dict[str, Path]:
    """返回本机上真实存在、且确实是 git 仓的自建仓。"""
    found: dict[str, Path] = {}
    for name in SELF_BUILT_REPOS:
        for parent in CANDIDATE_PARENTS:
            candidate = parent / name
            if (candidate / ".git").exists():
                found[name] = candidate
                break
    return found


def tracked_files(repo: Path) -> list[str]:
    """`git ls-files` 的 -z 输出。用 -z 是因为仓里迟早会有带空格的路径。"""
    result = _git(repo, "ls-files", "-z")
    return [entry for entry in result.stdout.split("\0") if entry]


# ---------------------------------------------------------------------------
# 判据：占位还是真货
# ---------------------------------------------------------------------------

# 只在【命中片段自身】里查，理由见模块 docstring 第 1 层。
PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "example",
    "placeholder",
    "dummy",
    "fake",
    "stub",
    "sample",
    "redacted",
    "changeme",
    "yourkey",
    "test",
    "todo",
    "xxxx",
)

# base64 / Bearer 的熵下限，单位 bit/char。
BLOB_ENTROPY_FLOOR: float = 3.5

# 明摆着的假号。前两个是工信部预留的示例号段与本仓 test_trade.py 现用的登录占位，
# 后几个是人手打字时的惯用假号；它们都躲不过下面的形态判据，列在这里是为了
# 让「为什么这个号不报」有个可查的出处。
KNOWN_DUMMY_NUMBERS: frozenset[str] = frozenset(
    {"13800138000", "13800000000", "12345678", "00000000", "88888888", "99999999"}
)


def shannon_bits(text: str) -> float:
    """按字符频率算香农熵。空串返回 0，让它落在下限以下被判成占位。"""
    if not text:
        return 0.0
    total = len(text)
    return -sum(
        (count / total) * math.log2(count / total) for count in Counter(text).values()
    )


def _longest_identical_run(digits: str) -> int:
    longest = 1
    current = 1
    # strict=False 是本意而非将就：两个序列长度差 1 就是「相邻两位配对」的写法。
    for previous, char in zip(digits, digits[1:], strict=False):
        current = current + 1 if char == previous else 1
        longest = max(longest, current)
    return longest


def _is_arithmetic_run(digits: str) -> bool:
    """整串等差（1234… / 9876…）。等差为 0 的情形由「全同」那条先接住。"""
    steps = {int(b) - int(a) for a, b in zip(digits, digits[1:], strict=False)}
    return len(steps) == 1 and steps != {0}


def is_placeholder_blob(token: str) -> bool:
    """长串（base64 / Bearer token）是不是占位。"""
    lowered = token.lower()
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        return True
    return shannon_bits(token) < BLOB_ENTROPY_FLOOR


def is_placeholder_number(digits: str) -> bool:
    """手机号是不是占位。"""
    if digits in KNOWN_DUMMY_NUMBERS:
        return True
    if len(set(digits)) <= 2:
        return True
    if _longest_identical_run(digits) >= 4:
        return True
    return _is_arithmetic_run(digits)


# ---------------------------------------------------------------------------
# 四条形状
# ---------------------------------------------------------------------------

# 写成 `(?:[A-Z0-9 ]+ )?` 而不是把 `RSA` / `EC` 逐个列出，是为了连
# OPENSSH / ENCRYPTED 这些变体一起收。副作用正好合用：这条正则【不匹配它
# 自己的源码文本】（`-----BEGIN ` 之后紧跟的是 `(`），所以本文件被扫到时不会
# 自己把自己判红。下面 test_the_scanner_does_not_flag_its_own_source 守这件事。
PEM_HEADER = re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")

# 字符类同时收标准与 URL-safe 两套字母表：URL-safe 的 `-_` 变体在 JWT 里就是常态。
LONG_BASE64 = re.compile(r"[A-Za-z0-9+/_-]{200,}={0,2}")

BEARER_TOKEN = re.compile(r"Bearer\s+([A-Za-z0-9._~+/-]{20,})")

# 键名必须后跟 `:` 或 `=`，否则 `hotel:` `italic:` 这类词内子串会把 CSS 也扫红。
# 分隔符要收全角 `：` 与 `＝`：中文键名几乎从不跟半角冒号，漏掉这两个字符
# 等于中文那半边键名全部失效（第一版就是这么漏的，`手机号：1……` 一条都不报）。
PHONE_KEY = re.compile(
    r"""(?: phone | mobile | msisdn | telephone | \b tel \b
          | 手机号 | 手机 | 号码 | 联系电话 )
        ['"]? \s* [:=：＝]""",
    re.IGNORECASE | re.VERBOSE,
)
CN_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
HK_MOBILE = re.compile(r"(?<!\d)[2-9]\d{7}(?!\d)")


@dataclass(frozen=True)
class Finding:
    """一条命中。

    刻意【不保存命中的文本】。这个对象会被拼进 pytest 的失败消息，而失败消息
    会进 CI 日志、进 issue、进聊天记录——把凭据从源码里搬进日志不算修好。
    定位靠 `文件:行号`，人自己去看。
    """

    repo: str
    path: str
    line: int
    shape: str

    def describe(self) -> str:
        return f"{self.shape}: {self.repo}/{self.path}:{self.line}"


def scan_text(text: str, *, repo: str, path: str) -> list[Finding]:
    """在一份文本里找四种形状。返回顺序即行号顺序。"""
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        shapes: list[str] = []

        if PEM_HEADER.search(line):
            shapes.append("PEM 私钥头")

        for blob in LONG_BASE64.finditer(line):
            if not is_placeholder_blob(blob.group()):
                shapes.append("长 base64")

        for bearer in BEARER_TOKEN.finditer(line):
            if not is_placeholder_blob(bearer.group(1)):
                shapes.append("Bearer token")

        if PHONE_KEY.search(line):
            for pattern, name in ((CN_MOBILE, "手机号(大陆)"), (HK_MOBILE, "手机号(香港)")):
                for number_match in pattern.finditer(line):
                    if not is_placeholder_number(number_match.group()):
                        shapes.append(name)

        findings.extend(
            Finding(repo=repo, path=path, line=number, shape=shape) for shape in shapes
        )

    return findings


@dataclass
class ScanResult:
    """一轮扫描的账。findings 之外还要记读了多少、跳过多少——见下面的空转守卫。"""

    findings: list[Finding]
    scanned: int
    skipped_binary: int
    skipped_large: list[str]


def scan_repo(repo: str, root: Path) -> ScanResult:
    result = ScanResult(findings=[], scanned=0, skipped_binary=0, skipped_large=[])
    for relative in tracked_files(root):
        path = root / relative
        try:
            size = path.stat().st_size
        except OSError:
            # 索引里有、工作树里没有（切分支切到一半、稀疏检出）。不是本用例的事。
            continue
        if size > MAX_BYTES:
            result.skipped_large.append(f"{repo}/{relative}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            result.skipped_binary += 1
            continue
        result.scanned += 1
        result.findings.extend(scan_text(text, repo=repo, path=relative))
    return result


# ---------------------------------------------------------------------------
# 探测器自身的体检
# ---------------------------------------------------------------------------
#
# 这一节存在的唯一理由：**「扫描零命中」和「扫描器坏掉」在结果上长得一模一样**。
# 上面那条真实扫描现在（2026-08-10）返回 0 条，把正则改成永不匹配它也返回 0 条，
# 绿灯不变。所以先用合成样本把探测器本身钉住。


def high_entropy_blob(length: int) -> str:
    """造一段像真凭据的串。

    用 `random.Random(种子)` 现场生成而不是往文件里贴一段字面量：贴字面量等于
    在一个「禁止出现凭据形状」的仓里亲手放一段凭据形状，日后必然有人误删。
    种子固定，因此这段串在任何机器上都一样，也就不会有「今天绿明天红」。
    """
    rng = random.Random(20260810)
    alphabet = string.ascii_letters + string.digits
    return "".join(rng.choice(alphabet) for _ in range(length))


def synthetic_number(prefix: str, length: int, seed: int) -> str:
    """造一个通得过占位判据的号码。

    和 `high_entropy_blob` 同一个理由，但这里更硬：手机号只有 8~11 位，写成
    字面量的话，这份文件自己就会被 `test_the_scanner_does_not_flag_its_own_source`
    判红——第一版正是这么红的（三处：两条样本 + 一条断言）。当时的诱惑是把本文件
    加进豁免名单，那等于给「扫描器不扫自己」开一个口子，日后谁把真凭据贴进
    tests/ 就再也扫不出来。改成现场生成，豁免名单一条都不需要。

    循环是必需的：随机数照样会撞上「末尾四个相同」这类占位形态，撞上就重摇。
    """
    rng = random.Random(seed)
    while True:
        digits = prefix + "".join(rng.choice(string.digits) for _ in range(length - len(prefix)))
        if not is_placeholder_number(digits):
            return digits


CN_SAMPLE = synthetic_number("13", 11, seed=1)
HK_SAMPLE = synthetic_number("6", 8, seed=2)

# 同理，PEM 头也拼出来而不是写成整段字面量。
PEM_SAMPLE = "-----" + "BEGIN RSA PRIVATE KEY" + "-----"

MUST_BE_FLAGGED: tuple[tuple[str, str], ...] = (
    ("PEM 私钥头", PEM_SAMPLE),
    ("长 base64", f'key = "{high_entropy_blob(320)}"'),
    ("Bearer token", f'headers = {{"Authorization": "Bearer {high_entropy_blob(48)}"}}'),
    ("手机号(大陆)", f'phone = "{CN_SAMPLE}"'),
    ("手机号(香港)", f'{{"phone": "{HK_SAMPLE}"}}'),
    ("手机号(大陆)", f"手机号：{CN_SAMPLE}"),
)

# 这些是本工作区真实在用的占位写法，逐条抄自现有测试。它们一旦开始报，
# 这条用例的寿命就以天计。
MUST_BE_SILENT: tuple[str, ...] = (
    'FAKE_AUTH = "TOKEN123"',
    'token="T", channel="C"',
    'client.login("13800000000", "PLACEHOLDER-PW", area_code="86")',
    'phone="00000000", channel="000"',
    '{"phone": "12345678"}',
    'password = "qwe123456"',
    'access_token="TK-NEW"',
    "def test_a_semantics_bump_that_touches_only_the_docstring_is_still_a_bump() -> None:",
    f'placeholder = "Bearer {"A" * 60}"',
    f'sample = "{"abc123" * 60}"',
    # 下面两条是标记词那一层【唯一】的覆盖，别删。
    #
    # 第一轮变异验证时我把标记词表整个摘掉，10 例仍然全绿 —— 因为当时列的占位样本
    # 全是低熵的（`"A"*60`、`"abc123"*60`），熵闸一层就接住了，标记词层从没被走到。
    # 一层没有任何用例覆盖的判据就是死代码，下一个人有充分理由把它删掉，而它恰恰
    # 是唯一挡得住「高熵的假凭据」的东西：手搓的示例 JWT 是随机字符，熵和真 token
    # 一模一样，只有名字里的 example / fake 能把它们分开。
    f'EXAMPLE_JWT = "example{high_entropy_blob(300)}"',
    f'auth = "Bearer fake-{high_entropy_blob(40)}"',
)


def test_every_synthetic_credential_shape_is_flagged() -> None:
    """六种形状逐条过一遍探测器。任何一条哑掉，都说明那条形状已经不设防。"""
    for shape, sample in MUST_BE_FLAGGED:
        findings = scan_text(sample, repo="synthetic", path="sample.py")
        shapes = [finding.shape for finding in findings]
        assert shape in shapes, f"形状 {shape} 漏了：{sample[:40]}… 只报出 {shapes}"


def test_the_project_own_placeholder_idioms_stay_silent() -> None:
    """占位凭据一条都不许报——误报是这条用例最可能的死因。"""
    for sample in MUST_BE_SILENT:
        findings = scan_text(sample, repo="synthetic", path="sample.py")
        assert not findings, f"占位值被误报：{[f.shape for f in findings]}"


def test_entropy_separates_random_tokens_from_repeated_filler() -> None:
    """熵闸的两侧各取一个极端，确认 3.5 这个阈值真的落在中间。"""
    assert shannon_bits(high_entropy_blob(320)) > 5.0
    assert shannon_bits("A" * 320) == pytest.approx(0.0)
    assert shannon_bits("abc123" * 60) < BLOB_ENTROPY_FLOOR


def test_placeholder_number_judgement_matches_its_stated_rules() -> None:
    """四条形态判据各来一例，外加白名单。

    `12341234` 这种「四种数字循环」**不在**判据里，是有意的：把「循环节」也算进去
    就会开始误伤真号（真号里出现 `1234` 重复并不稀奇），而它换来的只是少认出
    一种人手编的假号。判据宁可窄。
    """
    assert is_placeholder_number("00000000")        # 全同
    assert is_placeholder_number("12121212")        # 只有两种数字
    assert is_placeholder_number("12345678")        # 等差
    assert is_placeholder_number("87654321")        # 反向等差
    assert is_placeholder_number("13800000000")     # 末尾长串重复
    assert is_placeholder_number("13800138000")     # 白名单
    assert not is_placeholder_number("12341234")    # 见上：循环节不算占位
    assert not is_placeholder_number(CN_SAMPLE)
    assert not is_placeholder_number(HK_SAMPLE)


def test_a_phone_number_without_a_key_name_nearby_is_not_a_finding() -> None:
    """不带键名就不报。这是「每个 8 位整数都报一遍」与「能用」之间的分界。"""
    assert not scan_text(f"volume = {HK_SAMPLE}", repo="x", path="a.py")
    assert scan_text(f'phone = "{HK_SAMPLE}"', repo="x", path="a.py")


def test_the_scanner_does_not_flag_its_own_source() -> None:
    """本文件里写满了凭据形状的正则与合成样本，它必须不会自己把自己判红。

    这条不能靠下面的真实扫描代劳：新文件在 `git add` 之前不在 `git ls-files` 里，
    真实扫描根本看不到它——等提交完才发现自伤，那一次提交就已经是红的。
    """
    text = Path(__file__).read_text(encoding="utf-8")
    findings = scan_text(text, repo="vnpy_app", path=Path(__file__).name)
    assert not findings, [finding.describe() for finding in findings]


# ---------------------------------------------------------------------------
# 真实扫描
# ---------------------------------------------------------------------------


def test_scan_repo_finds_a_planted_credential_in_a_real_git_repo(tmp_path: Path) -> None:
    """在一个真的 git 仓里埋一把钥匙，`scan_repo` 必须挖出来。

    上面那些用例喂的都是内存里的字符串，走不到 `git ls-files` → `stat` →
    `read_text` 这一段。变异验证证明了这个缺口是真的：往 `scan_repo` 的读取
    循环里插一行 `continue`（等于「一份文件都不真读」），10 例照样全绿。
    这条用例把那段管道也钉住。

    顺带把「只看已跟踪」这条边界写成断言：同一个仓里再放一份**没 `git add`** 的
    同样内容，扫描必须看不见它。这正是 `.gitignore` 与本用例的分工线 ——
    未跟踪的归 ignore 规则管，进了索引的归这里管。
    """
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", "--quiet", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    (tmp_path / "tracked.py").write_text(f'phone = "{CN_SAMPLE}"\n', encoding="utf-8")
    (tmp_path / "untracked.py").write_text(f'phone = "{CN_SAMPLE}"\n', encoding="utf-8")
    assert _git(tmp_path, "add", "tracked.py").returncode == 0

    result = scan_repo("planted", tmp_path)

    assert result.scanned == 1
    assert [finding.path for finding in result.findings] == ["tracked.py"]
    assert result.findings[0].line == 1


def test_the_scan_actually_reaches_files_in_every_discovered_repo() -> None:
    """空转守卫：路径写错、`git ls-files` 失败时，扫描会安静地返回 0 条命中。

    vnpy_app 自己必须在场（这条用例就住在它里面），否则说明仓库发现整个坏了。
    """
    repos = discover_repos()
    assert "vnpy_app" in repos, f"连本仓都没找到，CANDIDATE_PARENTS 失效：{CANDIDATE_PARENTS}"
    for name, root in repos.items():
        assert tracked_files(root), f"{name} 一个已跟踪文件都没列出来"


def test_no_tracked_file_in_any_self_built_repo_carries_a_credential_shape() -> None:
    """本轮的主检查。红了就去看点名的 `文件:行号`，别把内容贴进任何日志。"""
    repos = discover_repos()
    findings: list[Finding] = []
    oversized: list[str] = []
    for name, root in sorted(repos.items()):
        result = scan_repo(name, root)
        findings.extend(result.findings)
        oversized.extend(result.skipped_large)

    assert not findings, (
        "已跟踪文件里出现凭据形状（只给位置，内容自己去看）：\n"
        + "\n".join(finding.describe() for finding in findings)
    )
    # 超限文件不算失败，但要说出来：它们是这次扫描没看的地方。
    assert not oversized or len(oversized) < 5, f"超过 {MAX_BYTES} 字节而未扫的文件：{oversized}"


# ---------------------------------------------------------------------------
# .gitignore 与凭据的存放位置
# ---------------------------------------------------------------------------

# 用 `git check-ignore` 而不是读 .gitignore 的文本比对：真正要断言的是
# 「这个文件名放进仓里会被忽略」这个行为，而它还取决于 .git/info/exclude、
# core.excludesFile、以及后面的规则会不会用 `!` 把前面的取消掉。比字符串
# 只能证明某一行存在过。
IGNORE_PROBES: tuple[str, ...] = (
    ".env",
    ".env.local",
    "secrets.pem",
    "client.key",
    "connect_usmart.json",
    ".claude/settings.json",
    "runtime.log",
)


def test_every_discovered_repo_ignores_the_credential_filenames() -> None:
    repos = discover_repos()
    missing: list[str] = []
    for name, root in sorted(repos.items()):
        for probe in IGNORE_PROBES:
            if _git(root, "check-ignore", "-q", probe).returncode != 0:
                missing.append(f"{name}: {probe}")
    assert not missing, "这些路径放进仓里不会被忽略：\n" + "\n".join(missing)


def test_the_vntrader_home_is_outside_every_repo() -> None:
    """真凭据只住在 `~/.vntrader`，而那里不在任何仓的路径之下。

    本机实测：`~/.vntrader` 解析为 `/Users/flink/.vntrader`，仓在
    `/Volumes/ORICO/...`，跨卷，`git add` 够不到。这条断言防的是日后有人为了
    「让配置跟着仓走」把 TRADER_DIR 挪进工作区——那一刻 connect_*.json 就成了
    一个 `git add -f` 之遥的东西。

    顺带断言它不是指向仓内的符号链接：`Path.resolve()` 会把 symlink 解开，
    所以这里比的是解析后的真实路径。
    """
    home = Path(os.path.expanduser("~/.vntrader")).resolve()
    for name, root in sorted(discover_repos().items()):
        assert not home.is_relative_to(root.resolve()), f"{name} 里面装着 {home}"

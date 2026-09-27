# wos-zotero-kit

Web of Science 检索 → RIS 导出 → Zotero 集合，监督式半自动。
摘自 ResearchFlow（提交 `68caac8`，2026-09-27），是复制品，不随原项目更新。

## 先看适用范围

**能用的前提（缺一条就别指望它自动跑通）：**

1. **机构 IP 直通 WoS**：在机构网络里打开 WoS 不用登录就能检索和导出。
   开着代理的话，WoS 相关域名（`webofscience.com`、`webofscience.clarivate.cn`、`clarivate.com`）必须走直连，
   出口不在机构 IP 上会被当成无权限，停在登录页。
2. **有 WoS 核心合集的导出权限**。
3. **Zotero 用云同步**，并有一个带写权限的 API key（<https://www.zotero.org/settings/keys>）。
4. Python 3.10+，能装 Playwright 的 Chromium。

**已验证过的只有一种环境**：Windows 11、天津大学教育网 IP 直通、`webofscience.clarivate.cn`、
一条 125 条结果的检索式，全自动跑通 1 次（另有 1 次被 captcha 拦下）。其余环境都没测过。

**不支持：**

- WebVPN、CARSI / Shibboleth 等机构登录的自动化。脚本遇到登录页只会停下等你手动登，
  WebVPN 改写后的网址可能让"是否到了结果页"的判断失效。
- 超过 1000 条的检索（WoS 单次导出上限），需要自己拆检索式。
- 只接受完整的 WoS 高级检索式（如 `TS=(...)`），不帮你把关键词拼成检索式。
- Zotero 本地 API、群组库。只写个人库的顶层集合。

**会遇到 captcha。** WoS 的风控按机构出口 IP 计，自动检索时间或被拦。
脚本不会绕过验证，只会停下来让你在浏览器里点过，再回终端按回车。
所以这是"多数时候不用管"，不是无人值守。同一个机构的人别频繁跑，会一起消耗这个 IP 的信誉。

**页面改版就可能失效。** 导出弹窗靠页面文字和结构定位（2026-09 的 WoS 界面）。
失效时脚本会截图报错、停下，不会乱点；截图在下载目录里。

## 安装

```bash
pip install -r requirements.txt
python -m playwright install chromium
# 可选：装了会自动使用，降低被识别为自动化的概率
pip install cloakbrowser
```

## 用法

```bash
export ZOTERO_API_KEY=...        # PowerShell: $env:ZOTERO_API_KEY="..."
export ZOTERO_USER_ID=...        # 数字 ID，在 API key 设置页能看到
python run.py --query 'TS=(("chronic obstructive pulmonary disease" OR COPD) AND acupuncture)' --collection COPD-acupuncture
```

| 参数 | 说明 |
|---|---|
| `--query` | 完整 WoS 高级检索式 |
| `--collection` | Zotero 顶层集合名，不存在就新建 |
| `--max-records` | 默认 1000 |
| `--dry-run` | 查重但不写 Zotero，先看会新建几条 |
| `--no-import` | 只导出 RIS |
| `--download-dir` / `--profile-dir` | 默认 `./wos-downloads`、`./.wos-profile`；profile 里存着浏览器登录状态，别删 |

流程：打开浏览器 → 填检索式提交 →（被拦就等你）→ 导出弹窗里选 Full Record、范围 1–N → 下载 RIS
→ 在整个 Zotero 库里按 DOI、标题加年份查重 → 已有条目加进集合、新条目新建 → 回读集合核对每一条都在。

退出码：`0` 全部成功；`3` 成功但有警告；`1` 失败。

## 怎么判断这次是真成功

看终端最后几行：

- `解析 N 条，其中 M 条带摘要`：N 应等于 WoS 命中数（超过 1000 时为 1000）；M 为 0 说明没切到 Full Record。
- `! ...` 开头的行都是警告。导出条数不足、没有摘要、回读集合发现缺条，都会列在这里。
- 每次入库在 `wos-downloads/manifests/` 下留一份 JSON，`missing_after_import` 应为空。

## 文件

```
run.py                       命令行入口
wos_zotero_kit/wos_browser.py   浏览器自动化（检索、验证检测、导出弹窗）
wos_zotero_kit/human_input.py   类人输入节奏
wos_zotero_kit/ris_parser.py    RIS → Zotero JSON
wos_zotero_kit/zotero_import.py 查重、建条目、加集合、回读核对（只用 Zotero Web API）
tests/                       离线测试：python -m unittest discover
SKILL.md                     给 AI 助手用的操作说明
```

## 许可证

MIT，见 `LICENSE`。

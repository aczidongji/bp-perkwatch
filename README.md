# bp-perkwatch — 本地 AI 福利监控

每天自动发现 AI 产品的**免费会员 / 免费额度 / 兑换码 / 学生优惠 / 限时试用 / 明确标价的促销**，
把条款结构化成固定字段，去官方页面核验，管理有效期，然后把当天值得看的几条
用一封邮件（正文 + `.md` 附件）推送给你。

它的唯一目标是**替你省时间**：不是摘要工具，不是新闻聚合。
一篇看不出「谁能免费拿到什么、到什么时候」的文章，对它来说就是噪声。

> **相关文档**：`README.md`（本文件）是完整的架构、选型理由与命令参考。
> 测试由 GitHub Actions 自动运行：338 条用例，零网络依赖，约 80 秒。

[![CI](https://github.com/aczidongji/bp-perkwatch/actions/workflows/ci.yml/badge.svg)](https://github.com/aczidongji/bp-perkwatch/actions/workflows/ci.yml)

---

## 1. 快速开始

```bash
cd ~/project/bp
bash scripts/bootstrap.sh            # 一条命令：uv + venv + 依赖 + 建库 + 迁移 + 来源清单 + .env + 自检
```

然后编辑 `.env` 填两个值（`DEEPSEEK_API_KEY`、`SMTP_PASSWORD`），再跑一次完整流程：

```bash
uv run bp run                        # 一条命令：采集 + 预筛 + 模型分析 + 官方核验 + 过期清扫
uv run bp report --stdout            # 看今天的日报
uv run bp report --send              # 发到 Gmail（正文 + .md 附件）
INSTALL_TIMERS=yes bash scripts/bootstrap.sh   # 开机自动跑（systemd 用户级定时器，无需 sudo）
```

没有 DeepSeek key 也能跑：`bp run` 会把文章全部入库并明确告诉你
「未配置 DEEPSEEK_API_KEY，本次跳过模型分析」，配好之后重跑即可，已入库文章不会重复花钱。

---

## 2. 数据流

```
┌──────────────────────── every night 18:05 (Beijing) ────────────────────────┐
 sources.toml │─▶ collect ─▶ dedupe x4 ─▶ prefilter ─▶ DeepSeek ─▶ verify│
   32 sources │    │    977 articles    free     off-peak    │           │
               └─────────────────────────────────────────────┼───────────┼────┘
               ▼                                             ▼
               SQLite (data/bp.db)  ──────────▶ daily report ─▶ Gmail 10:00
                                           (.md / .html + .md attachment)
```

> 图里**故意不用中文**：中文字符在等宽字体里占两格，混在框线里必然错位。
> 中文说明：`collect` 采集 32 个来源；`dedupe x4` 四级去重（见 §6）；
> `prefilter` 零成本关键词预筛；`DeepSeek` 低谷期抽取；`verify` 回访官方域名核验。
> `977 articles` 是真实入库量，`free` 指预筛不花 token，`off-peak` 指模型调用半价。

每个环节都只依赖前一个环节的产物，没有隐藏状态：文章、福利、证据、核验记录、
状态变更历史、通知记录全部落库，任何一步出错都可以单独重跑。

---

## 3. 组件选型：评估结论与实际做法

任务里点名要评估 7 个仓库。结论是**全部 SKIP**，但它们的成熟做法被当作库直接复用。

| 候选 | 许可 | 文档 | 部署成本 | 维护 | 结论 | 取而代之的做法 |
|---|---|---|---|---|---|---|
| **FreshRSS** | AGPL-3.0 | 很好 | PHP + Web 服务器 + 数据库 | 活跃 | **SKIP** | 它是"给人看的 RSS 阅读器"。需求是"给机器解析并结构化"，多套一层 PHP 反而要再从它的 HTML 里把内容抠出来。RSS 解析直接用 `feedparser`（30 行）。 |
| **RSSHub** | MIT | 很多但面向路由 | 需要常开 Node 服务 + Redis，公共实例不稳定 | 活跃 | **SKIP** | 只有公众号那一条路需要它，而公众号被明确排除在第一期之外（见 §9）。为几个路由常驻一个 Node 服务不划算。 |
| **Crawl4AI** | Apache-2.0 | 好 | 需要 Playwright/Chromium（数百 MB） | 活跃，API 变动频繁 | **SKIP** | 它的价值是 JS 渲染。实测 32 个来源里只有 JS 壳页面才需要它，而那些页面本来也不是文章。正文抽取用 `trafilatura`，**零浏览器**，实测跑满 32 来源没有一次需要 Chromium。 |
| **n8n** | fair-code（非 OSI） | 好 | Node + 常开服务 + 自己的 DB | 活跃 | **SKIP** | 编排逻辑就是"18:05 跑一次、失败明天补"。用 systemd timer + 一个幂等 `tick` 状态机更短、更可测、还能进版本库 review。 |
| **SearXNG** | MIT | 好 | 常开服务 + 自己的 DB | 活跃 | **可选，默认不装** | `SEARXNG_BASE_URL` 留空时关键词发现走 DuckDuckGo HTML 端点；你哪天有自己的实例，填上环境变量即可切换，代码不用改。 |
| **Mp2RSS** | MIT | 少量 | 需要第三方中转 | 不活跃 | **SKIP** | 公众号第一期不碰（用户确认）。另外它依赖第三方抓取服务，链路上多一个不透明的信任点。 |
| **WeRSS** | 未明确 / 需要部署 | 少量 | Docker + 需要微信侧凭据 | 不活跃 | **SKIP** | 同上；且它需要真实微信身份参与抓取，与「不代持账号凭据」的边界冲突。 |

**所以整套系统只有 1 个容器以外的运行时依赖**：Python 3.12 + `uv`。
没有 Docker、没有 PHP、没有 Node、没有 Redis、没有 Chromium、没有额外数据库。
（Docker 只在"真有必要"时才引入，这里没有必要。）

---

## 4. 为什么值得自己写这几千行

三件事在现成项目里没有，而它们是这个系统的核心，不是外围：

1. **信任边界是机械强制的，不是提示词。** 「已确认」在 SQLite 层就有两个
   `BEFORE INSERT/UPDATE` 触发器挡着（`migrations/0001_init.sql`），
   任何代码——包括未来我自己写的 bug——都无法在没有新鲜官方核验记录的情况下写出 `confirmed`。
2. **反幻觉不是许愿。** 模型每条非 null 字段必须给逐字引文；引文要能在原文里
   一字不差找到，否则该字段被清空、整条置信度封顶（`src/bp/models.py`）。
   链接同理，凭空拼出来的官网会被识别出来（`_invented_urls`）。
3. **去重的目的是"同一活动的多个来源合并成一条"。** 四层设计（§6）针对的是
   "同一活动被 5 篇不同的文章写"，而不是"同一篇文章抓了两次"。

---

## 5. 目录结构

```
config/       sources.toml(32) official_domains.toml keywords.toml aliases.toml
migrations/   0001_init.sql
src/bp/       采集 collect/{rss,web,search}.py
              识别 llm.py models.py prefilter.py
              存储 store.py(去重/合并) normalize.py lifecycle.py verify.py
              产出 report.py notify/email.py query.py
              运行 pipeline.py scheduler.py cli.py config.py db.py fetch.py timeutil.py
systemd/      bp-tick.{service.in,timer}
templates/    report.md.j2 + report.html.j2（日报的 Markdown 正文与 HTML 正文）
tests/        10 个文件 343 条用例
scripts/      bootstrap.sh install-timers.sh
data/         bp.db（WAL，已 gitignore）
reports/      perk-report-YYYY-MM-DD.md
```

---

## 6. 四层去重（`src/bp/store.py`）

| 层 | 判据 | 作用 |
|---|---|---|
| 1 文章 URL | 归一化 URL：微信 `/s/` 短链去掉 `scene`、`chksm` 等跟踪参数 | 同一篇公众号文章的不同分享链接 |
| 2 文章内容 | 标题不敏感的内容哈希 | 转载洗稿、换标题的搬运 |
| 3 别名表 | `article_aliases`：URL 不同但内容哈希相同 | 保留全部来源，指回同一个规范条目 |
| 4 福利身份 | `canonical_key` = 产品 + 福利类型 + 领取入口/规则链接 + 额度签名 | 跨文章识别同一活动 |

第 4 层的合并阶梯是显式排序的，并且**拒绝跨额度冲突合并**：
「送 100 次」和「送 1000 次」是两个不同的福利，绝不合并成一条。
`UNIQUE(url_norm, content_hash)` 让"来源改了原文"变成一条新的 revision 行，
而不是原地覆盖——历史因此可查。

---

## 7. 状态机与生命周期

```
                 ┌──────────── 官方域名 + 页面仍可领取 + 记录未过期 ────────────┐
                 │                                                              ▼
  抽取入库 → pending(待核实) ──▶ confirmed(已确认) ──▶ expired(已过期)      [需要触发器放行]
                 │                    │
                 │  第三方转载 / 登录墙 / 付费墙 / 验证码 / 证据不足
                 ▼                    ▼
             needs_review(待核实+原因) dubious(存疑)      invalid(无效)
```

- 过期由 `ends_at` 每天清扫（`lifecycle.sweep_expired`），**过期即清空 `verification_id`**，
  所以一条过期记录不可能第二天又被当成"已确认"。
- `ends_at == 今天` 仍然有效；`ends_at` 为 NULL 时**绝不猜测过期**。
- 「即将截止」= `EXPIRING_SOON_DAYS`（默认 7 天）窗口内。
- 缺领取入口链接 → 标记待核实并给出原因，而不是直接丢弃。
- 「已领取」是独立字段，与状态正交：领过了活动依然可能仍在有效期。

---

## 8. 调度：为什么是 15 分钟一次的 `tick`，而不是 18:05 一个 cron

`bp tick` 是一个幂等状态机，每 15 分钟醒来一次，自己判断"今天该不该做、做到哪一步了"，
用 `night_run_YYYY-MM-DD` / `report_sent_YYYY-MM-DD` 两个水位标记记录进度。
这样设计直接是为了满足你的三条要求：

| 你的要求 | 实现 |
|---|---|
| 每天 18:00 开始，周末节假日照跑 | `PIPELINE_START_HOUR_BJ=18` / `MINUTE=5`，**不看星期**（18:05 正好跨过 14:00–18:00 高峰，之后整夜半价） |
| 关机几天后开机只发最新一份 | 补跑只生成当天日报；错过的旧日报行打 `superseded_at`，不再发送 |
| 10:00 之后才开机 → 开机即发 | `Persistent=true` + `OnBootSec=3min`，开机第一次 tick 就命中"已过 10:00 且今天没发" |
| DeepSeek key 只在低谷期用 | 高峰 = 北京 09:00–12:00、14:00–18:00（周一~周五，法定节假日全天按低谷算）。分析批次一旦进入高峰立即停止，剩余文章留到当晚。`bp doctor` 会打印当前是不是高峰。 |

一次失败的 SMTP 发送**不会**消耗当天的水位标记，下次 tick 自动重试；
`bp tick --dry-run` 只打印计划、不改状态。
调休上班的周六被**故意**当作低谷：DeepSeek 的官方定义是 Mon–Fri，多省一半钱。

---

## 9. 成本

- **关键词预筛免费**：不调模型、不联网。实测首轮采集后库内 574 篇文章，
  随机抽 120 篇判定：75 篇被 0 成本跳过（"无触发词"或"命中负面词"），
  45 篇送模型——模型调用量只有采集量的三分之一左右。
- **低谷价**：分析只在 18:05 之后跑，DeepSeek 高峰价是低谷价的 2 倍。
  高峰窗口按北京时间 9:00-12:00、14:00-18:00，周一至周五，**不含中国法定节假日**
  （节假日也算低谷）。18:05 起跑按时间就不在高峰窗口内，所以夜间批跑与节假日数据无关；
  它只影响"白天补跑"——法定节假日那几天，补跑会被推迟到当晚 18:05。
- **节假日数据的边界**：放假安排来自 `chinese_calendar`，按年打包（当前版本覆盖
  2004-2026）。**2027 年的国务院放假通知要到 2026 年 10~11 月才发布**，在那之前
  没有任何版本可升级，缺数据不是 bug，是数据还没存在。缺数据的年份退化为"只看周末"
  （工作日按工作日算，约 96% 的情况是对的）。`bp doctor` 会打印覆盖范围，
  不含今年时明确说出代价和解法；等新数据发布后升级依赖即可消除。
- **单晚上限**：`ANALYSE_BATCH_LIMIT=80` 限制定时器每晚送模型的文章数；积压几百篇时
  它就是当晚账单的上限。手动 `bp analyse` 不受约束（自己决定 `--limit`）。设 0 = 不限。
- **提示缓存**：`SYSTEM_PROMPT` 是不变的静态块，作为 system 消息发送，
  只有文章正文在变，所以缓存命中时输入部分按 cache 价计费。
- **不重复付费**：`analyze_state` 保证一篇文章只送模型一次；重跑管道对已分析内容零成本。
- `DEEPSEEK_MAX_OUTPUT_TOKENS=4000`、正文最长 24,000 字符截断。
- **关掉思考**：`deepseek-flash` 默认开推理，实测同一篇文章 5727 → 370 token
  （请求里 `thinking: {"type": "disabled"}`）。不关的话输出预算全花在推理上，
  正文是空的，看起来像 API 故障。

---

## 10. 安全边界

四条硬约束，每条都指到具体实现位置：

| 约束 | 机械强制点 |
|---|---|
| 不得绕过登录验证、验证码或付费墙 | `fetch.py` 只有 GET，从不发 body、从不注入凭据/cookie；`detect_gate` 命中登录/验证码/付费墙 → 记录门槛类型并**停手**，福利转 `dubious`，原因写明「未尝试绕过」。HTTP 403 也归到门槛而不是"链接坏了"。 |
| 不得自动提交支付信息或擅自操作用户账号 | 不存在任何 POST/PUT 表单路径；`.env` 只保存你自己签发的应用专用密码，只用于向**你自己的**邮箱发信。 |
| 禁止仅凭公众号文章或 LLM 判断就标记为「已确认」 | `official_domain_match` 只可能来自 `official_domains.toml` 白名单或搜索结果佐证，**永远不由模型给出**。数据库触发器 `CONFIRM_GUARD`（INSERT 和 UPDATE 两侧）独立于 Python 再拦一次：没有"官方域名匹配 + 页面可达 + verdict=available + 记录未超 `VERIFICATION_MAX_AGE_DAYS`"的核验行，`confirmed` 写不进去，直接 `IntegrityError`。 |
| 缺少关键证据时不得编造字段 | 提示词要求无证据填 null；`strip_unsupported_fields` 把每条引文回原文逐字回溯，找不到就清空该字段并封顶置信度；引文最短长度有 schema 校验；`benefit_type` 允许为 NULL（待核实），**不会**为了过 schema 而硬猜一个类型。 |

另外：`robots.txt` 按 host 生效并遵守；robots 文件读不到时才当作宽松处理（这是通行做法，
避免一个坏主机卡死整轮）。所以本系统只读取公开页面。

---

## 11. 输出

**邮件**（Gmail，正文 + 附件）：`multipart/mixed`
= `text/plain` + `text/html`（`templates/report.html.j2`）+ `text/markdown` 附件
`perk-report-YYYY-MM-DD.md`。需要 Google 账号开启两步验证后，在
<https://myaccount.google.com/apppasswords> 生成 16 位**应用专用密码**（普通密码不行）。

日报六个板块：新发现的候选福利 / 规则有更新 / 已确认且仍在有效期内 / 即将截止 /
本周期内过期或失效 / 需要你人工判断。每个板块内部**按产品分组**（同一产品的免费档、
学生价、兑换码归到一个产品标题下，标题写「N 条 · M 个产品」），每条的第一行就是
**到期倒计时**：`今天截止` / `明天截止` / `剩 3 天` / `未注明截止日` / `已过期 3 天`，
HTML 版按紧迫度上色（≤2 天红、≤7 天橙），Markdown 版紧迫项加粗。
核验时间显示为北京时间而不是 UTC 原始串。
**一条福利在一封日报里只出现一次**：六个板块是同一批福利的六个视角，天然重叠，
所以按「即将截止 › 已过期 › 已确认 › 新发现 › 规则更新 › 需人工判断」的优先级归到
唯一一个板块，板块末尾用一句话说明折掉了多少条，避免把板块数读成总数。跨天同理：`needs_review` / `recently_expired` 按 `updated_at` 开窗，而空转的 tick 与
转载同一套规则的重复条目**不再改写 `updated_at`**，所以同一条福利不会天天被顶回报告里。
`REPORT_REPEAT_LIVE_LIST=false` 可切换为"每条福利只在状态变化时出现一次"。

### 什么叫"福利"（两道闸门）

模型自报的 `is_promotion` **不作数**，实测它把两类东西都判成了促销：

- 产品发布稿（WeatherNext 气象模型、Threat Signals、DeepMind 科学组合）
- **常驻套餐说明**：`Free plan includes 0.5 GB of ingestion per day`、
  `Free users are limited to 2000 completions`、`Max 含 $100/month credits`

第二类最迷惑——它有额度、看着很具体，但它是**产品的固有属性**：常驻、无条件、
写在定价页上，读者无从下手。所以判断标准是：

> **促销活动有边界（怎么写到手 / 什么时候截止），套餐内容没有。**

两道机械闸门，词表都在 `config/keywords.toml`，改词不改代码：

| 层 | 函数 | 规则 | 实测效果 |
|---|---|---|---|
| 文章级（送模型前） | `prefilter.score` | 价格信号 **且** 主体信号 **且** 获取/边界信号，三者缺一不可 | 977 篇真实文章：416 → **229** 篇送模型，省 45% |
| 福利级（入库前） | `prefilter.looks_like_promotion` | 自带获取/边界词 **或** 起止日期 **或** 领取入口；且不得只是套餐说明 | 真实库 28 条：留 **12**，丢 16 |

留下的 12 条是：学生/教师优惠 ×7、折扣 ×3、免费一年 ×1、限时高档位 ×1。
丢掉的 16 条里，**Hugging Face 的 "$20 compute credits" 是唯一有争议的一条**
——它确实像赠送，但原文没写怎么拿到，这个闸门看不见"怎么拿到"就只能判掉。
宁可漏报也不把套餐说明当福利，这是有意的取舍。

### 日报长什么样（真实摘录）

下面是从一封真实日报里摘出来的（条目已精简，来源链接原样保留）。
它直接说明了这个系统的价值：**16 条被判为"不是促销"的条目带着原因出现一次然后消失，
剩下 12 条才是你真的可以去领的。**

```
# AI 福利日报 — 2026-10-16

| 状态 | 数量 |
|---|---|
| 待核实 | 11 |
| 已确认 | 0 |
| 已过期 | 0 |
| 无效 | 16 |
| 存疑 | 1 |

## 需要你人工判断（12 条 · 11 个产品）

### GitHub Copilot（2 项）
#### 学生优惠 2 — 未注明截止日
- **状态**：待核实
- **权益**：经过验证的学生可免费使用 Copilot 功能
- **新老用户限制**：面向经过验证的学生
- **门槛**：学生认证需要｜绑卡未说明｜邀请好友未说明
- **可信度**：90%｜最后核验：10-15 19:39
- **待处理原因**：缺少领取入口链接
- **原文（共 1 个来源）**：
  - https://docs.github.com/en/copilot/get-started/plans

### Kiro
#### 未定性 — 未注明截止日
- **状态**：待核实
- **权益**：免费使用一整年 Kiro，且无需信用卡
- **适用地区**：全球 18 个国家
- **新老用户限制**：全球 18 个国家、132 所大学的在校学生，有 .edu 学生身份且学校在名单内大学
- **门槛**：学生认证需要｜绑卡不需要｜邀请好友未说明
- **官方规则**：https://www.aboutamazon.com/news/aws/amazon-expands-free-kiro-access-students-universities
- **可信度**：40%｜最后核验：10-15 19:53
- **待处理原因**：缺少领取入口链接

## 本周期内过期或失效（16 条 · 14 个产品）

### Cloudflare Traces
#### 免费额度 — 未注明截止日
- **状态**：无效
- **权益**：Free plan includes 0.5 GB of ingestion per day with 7 days retention
- **额度**：0.5 GB of ingestion per day
- **可信度**：90%｜最后核验：10-15 19:39
- **待处理原因**：系统规则复核：这只是产品/套餐的固有属性，不是促销活动
- **原文（共 1 个来源）**：
  - https://blog.cloudflare.com/cloudflare-tracing/

### GitHub Copilot（3 项）
#### 免费会员 1 — 未注明截止日
- **状态**：无效
- **权益**：GitHub Copilot Free users are limited to 2000 completions and 50 chat requests (including Copilot Edits).
- **额度**：2000 completions and 50 chat requests
- **可信度**：90%｜最后核验：10-15 19:39
- **待处理原因**：系统规则复核：这只是产品/套餐的固有属性，不是促销活动
- **原文（共 1 个来源）**：
  - https://github.com/features/copilot/plans
```

两条细节值得注意：`Free plan includes 0.5 GB` 和 `limited to 2000 completions`
都被判成"套餐固有属性"而不是福利——**前者是免费档的配额，后者是上限而不是赠送**；
而"学生认证后可免费使用 Copilot"留下了，因为它写明了怎么拿到。

### 词表改了之后怎么办（`bp gate`）

闸门是启发式，词表在 `config/keywords.toml` 里随时可以改。但它以前只在**抽取那一刻**
生效，已经入库的旧条目不会重新评估——而 `needs_review` 板块现在会持续列出，所以一条被
误判成福利的套餐说明会天天回来。

`bp gate` 就是补这个洞的：按当前词表把库里 `pending` / `dubious` 的福利重新判一遍。

```bash
bp gate --dry-run     # 先看会判掉哪些，不改数据
bp gate               # 正式执行
```

判掉的**不删除**，而是置为 `invalid` 并在日报里以「本周期内过期或失效」露面一次，
`review_reason` 写明「系统规则复核：这只是产品/套餐的固有属性，不是促销活动」，
状态历史、版本、引文、来源全部保留。**官方确认过的福利绝不碰**——`confirmed` 不在复核范围里。
它每晚会随 `bp run` 自动执行一次，所以收紧词表后无需手动跟进。

> **没有 Web 后台。** 按 2026-10-16 的决定，交付物就是"每天一封能收到、能看附件的
> 邮件"，网页层已整体删除：`webapp.py`、五个页面模板、`bp web` 命令、
> `bp-web.service`、fastapi / uvicorn / python-multipart 依赖。它上面的每个动作都有
> CLI 等价物，所以删掉不丢任何能力：筛选列表 → `bp ls --status ...`、详情与引文 →
> `bp show <id> --history`、标记已领取 → `bp mark <id>`、人工改状态 →
> `bp status <id> ...`、导出 → `bp export --output perks.csv`。
> 人工改状态是允许的（记录 `changed_by='human'`），但命令行点「已确认」同样会被
> 守卫拒绝——CLI 也逃不过触发器。

---

## 12. 命令参考

| 命令 | 作用 |
|---|---|
| `bp init` | 建库 + 迁移 + 导入来源清单 + 写 meta（可重复执行） |
| `bp run` | 单次完整流程（采集 → 分析 → 核验 → 清扫 → 报告水位） |
| `bp collect` | 只采集，不调模型（零成本） |
| `bp analyse [--limit N] [--force]` | 只处理积压的 `pending` 文章 |
| `bp verify [--limit N]` | 只对未核验的福利做官方核验 |
| `bp gate [--dry-run] [--limit N] [--show N]` | 按当前关键词表重新判断库里已有的福利（改词表后跑） |
| `bp tick [--dry-run]` | 定时器入口，幂等状态机 |
| `bp report [--stdout] [--send/--no-send] [--dry-run-send]` | 生成/发送日报 |
| `bp sources list\|check [id]\|add\|enable\|disable\|remove` | 来源管理；`check` 真的抓一遍每个来源并报告 |
| `bp ls [--product X] [--status pending] [--type free_credits] [--unclaimed]` | 查询福利 |
| `bp show ID` / `bp mark ID` / `bp status ID dubious --reason ...` | 详情 / 标记已领取 / 人工改状态 |
| `bp export --format csv\|json [--out FILE]` | 导出 |
| `bp stats` / `bp doctor` | 统计 / 自检（配置、schema、当前是否高峰、**节假日数据覆盖范围**） |
| `bp mail-test` | 发一封带 `.md` 附件的自检邮件 |

---

## 13. 关键配置

`.env`（从 `.env.example` 复制，示例文件不含任何真实密钥；`.env` 已 gitignore）：

```
DEEPSEEK_API_KEY=            # 必填才能分析
DEEPSEEK_MODEL=deepseek-flash
LLM_BATCH_DEADLINE_HOUR_BJ=8          # 过了这个点停止调用模型
FORCE_ANALYSIS_ANYTIME=false  # true = 高峰也跑（多花一倍钱）

SMTP_HOST=smtp.gmail.com  SMTP_PORT=465  SMTP_USE_SSL=true
SMTP_USERNAME=...  SMTP_PASSWORD=<应用专用密码>  REPORT_TO=...

PIPELINE_START_HOUR_BJ=18  PIPELINE_START_MINUTE_BJ=5  REPORT_SEND_HOUR_BJ=10
VERIFICATION_MAX_AGE_DAYS=7  EXPIRING_SOON_DAYS=7
REPORT_REPEAT_LIVE_LIST=true
MAX_ITEMS_PER_SOURCE=40  FETCH_DELAY_SECONDS=1.5  HTTP_TIMEOUT_SECONDS=25
SEARXNG_BASE_URL=        # 填了就用你自己的实例做关键词发现
SEARCH_DENY_DOMAINS=     # 逗号/分号/空格分隔的排除域名
```

调这些不需要改代码：`config/official_domains.toml`（产品 → 官方注册域名，决定能不能确认）、
`config/keywords.toml`（预筛触发词/主体词/负面词）、`config/sources.toml`（来源清单）、
`config/aliases.toml`（产品别名归一化）。

---

## 14. 测试与实跑台账

### 已验证（自动化测试，311 条用例，本地全绿）

```bash
uv run pytest            # 311 passed in ~107s
```

| 领域 | 用例 | 覆盖内容 |
|---|---|---|
| 去重与合并 `test_dedupe.py` | 22 | 四层去重各自命中；微信跟踪参数归一；转载识别为别名；**不同额度不合并**；来源更新产生 revision 而历史不丢；同一福利多来源聚合；**转载同一套规则不改写 `updated_at`**（否则每晚几百条重复会把日报时间窗一直开着）；置信度更高才记录 |
| 过期检测与倒计时 `test_lifecycle.py` | 30 | `ends_at` 过期即关闭并清空核验；`ends_at=今天` 仍有效；NULL 日期不猜过期；已确认的照样过期；无效的不重复清扫；剩余天数含"本月底"→ None；即将截止窗口边界；缺链接转待核实；**重复打标是空操作**（不改 `updated_at`、不取消标记）；已领取与状态正交；**倒计时措辞**（今天/明天/剩 N 天/未注明截止日/已过期 N 天）与过期清扫共用同一个 `days_remaining` |
| 模型 JSON 解析 `test_extraction.py` | 72 | 裸 JSON / ```json 围栏 / 单行围栏 / 夹杂说明文字；不可解析报错；未知键与非法枚举报错；100 条上限截到 12；中文日期与 `2026.12.31` → ISO；"月底/下周" → None；`confidence=80` → 0.8；引文回溯清空无据字段并封顶置信度；凭空链接识别；短文本在调用模型前就拒绝；strict schema 自洽（required=全部属性、additionalProperties=false、无 $ref） |
| 通知去重 + 邮件 + 日报排版 `test_notify.py` | 46 | 首次 True/重复 False；status·ends_at·claim_url·quota 变化会再次通知；纯排版改动不会；`dedupe_key` 在 SQL 层唯一；日报六板块；同日重生成替换而非堆叠；HTML 转义不可信文本；状态表每个状态只出现一次（中文标签，不与英文键并存）；**同一产品的多条福利归到一个产品标题下、顺序按首次出现、Markdown/HTML 两版标题一致**；**一条福利同时符合多个板块时只渲染一次**（按优先级归属，板块数不再大于总数，折叠数量在报告里说明）；MIME 结构与 `.md` 附件文件名/内容逐字节校验；SSL 与 STARTTLS 两条握手序列各只发一封；发送失败返回结果而不抛异常；`bp report --dry-run-send` 只排版不碰网络 |
| 官方核验 `test_verification.py` | 31 | 直写 SQL 的 `confirmed` 被 `CONFIRM_GUARD` 拒绝（INSERT/UPDATE 两侧）；文章 + LLM 不足以确认；第三方域名不足以确认；过期记录不足以确认；新鲜官方 available 才够；过期/无效无需凭据即可写入；时效窗口读 `meta`；白名单子域匹配；404 → 无效；登录/付费/验证码 → 存疑并写明未绕过；403 归为门槛；每个查过的 URL 都落核验记录；claim 与 rules 同页只抓一次 |
| 调度状态机 `test_scheduler.py` | 25 | 18:05 前不跑（昨天已跑）；18:10 跑一次并打水位；第二次 tick 空闲；积压在 12:30（低谷）补跑；工作日高峰拒绝补跑除非显式强制；周末立即补跑；dry-run 不消耗水位；10:00 前不发信；每天只发一封；13:40 开机立即发；**几天没开机只发最新一份**且旧的打 superseded；SMTP 临时失败不留水位并在 10:20 重试；未配置邮箱不刷屏；每 tick 都做过期清扫；**定时器每晚送模型的文章数受 `ANALYSE_BATCH_LIMIT` 限制**（默认 80，设 0 为不限，手动 `bp analyse` 不受约束） |
| 抓取层 `test_fetch.py` | 25 | robots 按 host 生效（子域各自规则）；全站 Disallow 时不发正文请求；robots 读不到时宽松；同 host 只取一次；登录/验证码/付费墙识别；无 scheme 的 URL 归一；空 URL 不碰网络；JS 壳页面（15 字符）判定为不可用；RSS `content:encoded` 的 HTML 片段转纯文本（标签/脚本不留存）；双重转义实体 `&amp;quot;` 还原成真正的引号；**正文保留 `[锚文本](url)`**，否则 `claim_url` 无据可引 |
| 配置与路径 `test_config.py` | 6 | 相对 `DB_PATH`/`REPORTS_DIR` 锚定到项目根；绝对路径原样保留；环境变量与构造参数两条路径结果一致；从别的目录打开的是同一个库 |
| 归一化 `test_normalize.py` | 26 | 文本/URL/产品/额度签名归一化的稳定与幂等 |
| 入库闸门 `test_pipeline.py` | 9 | 模型自报 `is_promotion=true` 时，只有产品描述、没有入口/时间窗/额度也没写怎么拿到的**产品发布稿被丢弃**（WeatherNext / Threat Signals 类）；带领取入口的、只写了"学生认证后可免费使用"的真福利都保留；被丢弃时文章仍标记 done 不反复送模型 |

### 已实跑验证（2026-10-15 在本机真实执行）

- ✅ `bash scripts/bootstrap.sh` 一条命令完成初始化（uv → venv → 依赖 → .env → 迁移 → 32 来源 → doctor），可重复执行且不动已有 `.env`
- ✅ `bp sources check`：32/32 来源真实可达（RSS 5 条 / 网页正文 / 搜索 5 条各有产出）
- ✅ `bp collect` 真实采集：554 条目 → 534 篇新文章、1 篇 revision、9 重复、10 别名；14 张表、WAL、迁移记录正确
- ✅ `bp run`（无 key）优雅降级：文章全部入库，明确提示跳过分析，零成本
- ✅ 无 key / 高峰时段 `bp tick` 的两条拒绝路径都按预期输出原因
- ✅ `bp report` / `bp export` / `bp stats` / `bp doctor` / `bp mail-test`（MIME 结构含 `.md` 附件）本地全部实跑通过
- ✅ 日报真的进了 Gmail 收件箱，附件 `perk-report-YYYY-MM-DD.md` 可下载、内容完整
- ✅ **分析→入库→核验→日报全链路**：用桩模型（替掉 `_call`，其余代码与真实运行完全一致）
  对着本晚真实采集的文章跑通，产出 7 条待核实福利，日报六个板块与状态表渲染正常。
  桩模型两次越界（给非推广文章判 `is_promotion`、编造文章里没有的产品名）都被 strict schema
  与引文回溯拦下 → 说明反幻觉护栏确实在起作用，而不是只写在文档里
- ✅ **systemd 定时器实跑一轮完整 tick**（20:30:34 → 20:37:28）：32/32 来源正常、555 条目、
  新文章 9 / 改版 34 / 重复 500 / 别名 12，免费预筛跳过 73、送模型 7（受 `ANALYSE_BATCH_LIMIT` 约束）、
  新福利 3、核验 4 条、失败 0，`prompt 12,339 / completion 1,296 / cached 4,480`，
  水位 `night_run_2026-10-15` 正确写入（同晚后续 tick 不再重复跑）
- ✅ 用真实数据复查日报状态表：修 key 混用后只剩 5 行中文标签（`待核实 | 7`），
  不再同时出现 `pending | 0`
- ✅ **真实官方核验**：对着真站跑了一遍 `bp verify` — cursor.com 官方定价页 → `confirmed`
  （`official_domain_match=1`、`verdict=available`）；第三方转载域名 → 不确认；
  chatgpt.com 登录墙 403 → `dubious` + 「需人工登录确认」，**没有尝试绕过**
- ✅ **真实 DeepSeek 调用**（不是桩模型）：低谷期两批实测 —— 66 次调用 / 134k prompt token /
  8k 输出 token，以及 19 次调用 / 83,306 prompt / 1,226 输出 / 9,728 命中缓存；
  全部 HTTP 成功、JSON 全部可解析，累计产出 24 条福利（100 条引文全部通过原文逐字回溯）
  （如 GitHub Copilot Free「2000 completions / 50 chat requests」、Cloudflare Logpush
  「每月 25 GB 免费导出」、Kiro 学生一年免费「无需信用卡」）；
  预筛跳过 92 篇零成本；置信度 60%–90% 分布合理
- ✅ 反幻觉闸门在真模型下确实在工作：3 条福利因为「产品名/权益在原文里找不到逐字引文」
  被丢弃并计数（`bp analyse` 现在会打印这一行），而不是写进库里
- ✅ **链接修复后官方核验真正有活干了**：`bp verify` 对两条带链接的福利写出了
  `verification_runs` 记录 —— Kiro 抓到 aboutamazon.com 官方公告（HTTP 200、
  `verdict=available`），但域名不在白名单且搜索未佐证 → **保持 pending 不升级**；
  MiniMax 命中白名单（`official_domain_match=1`）但页面是验证码门槛 →
  `dubious` + 标明未绕过
- ✅ 真实模型逐字引用了文章里的链接（不再是 null）：`rules_url` 引文的原文位置可回溯，
  证明 §14 第 24 条的正文保链接修复端到端生效
- ✅ **真实 Gmail 投递**：用你的应用专用密码（16 位）`bp mail-test` 认证成功、Gmail 接受投递，
  邮件**进了收件箱而不是垃圾箱**，发件人已加白名单。MIME 结构：`multipart/mixed` →
  `alternative`(plain+html) + `text/markdown` 附件 `perk-report-2026-10-15.md`
- ✅ systemd 单元：渲染后路径与可执行文件全部存在，`systemd-analyze verify --user` 无告警
- ✅ DeepSeek 计费口径与模型名核对：高峰 = UTC 01:00–04:00、06:00–10:00（= 北京 09–12、14–18），
  仅周一~周五，高峰价是低谷价的 2 倍；当前模型名 `deepseek-flash`，
  旧名 `deepseek-chat` / `deepseek-reasoner` 已于 2026-07-24 下线

实跑过程中发现并修掉的问题（都由真实执行暴露，不是读代码读出来的）：

1. `trafilatura.extractors` 导入不存在 → 采集一 import 就崩
2. `REPORT_TO` 被 Settings 静默忽略 → 邮件永远发不出去
3. robots.txt 按注册域名查 → 白名单里的学生页被误判为禁止抓取
4. `NOT NULL: perks.benefit_type` → 无证据的类型被硬猜；改为允许 NULL 并标待核实
5. 合并分支 `UPDATE` 少绑一个参数 → 存储层直接崩
6. 两个无类型福利撞 `canonical_key` UNIQUE → 回退键改用内容哈希
7. 首次日报永远为空（`since or now` 把全部历史过滤掉）
8. SMTP 失败仍消耗当天水位 → 开机后不再重试；`--dry-run-send` 也消耗水位
9. 缺 key 时静默返回 0 → 现在打印明确原因
10. `bp init` 提示的 `bp run --once` 不是真实参数
11. `sources check <id>` 忽略 id 跑全量；`urljoin("https://", host)` 造出 `https:///host`
12. 同一页面同时是 claim 与 rules 时被抓两次
13. 测试套件每例重建数据库要 fsync，10 分钟跑不完 → 测试用 `journal_mode=MEMORY` +
    `synchronous=OFF`，生产 `synchronous=NORMAL`，现在 92 秒
14. 死来源一直报 OK：JetBrains 学生页抓回 15 字符、Google AI Student 抓回 2 字符（都是 JS 壳）
    → `MIN_USABLE_CHARS = 200`，不足就明确写「页面需要 JavaScript」，并换掉 3 个抓不到的站
15. RSS 的 `content:encoded` 原样入库 → 首轮 574 篇里 210 篇满是标签，引文出现
    `</p><blockquote>&quot;…`，token 虚增；改为入库前转纯文本（`html_to_text`）。
    顺带发现两条路径都把双重转义的 `&amp;quot;` 留在正文里，补了一遍实体还原
    （真实 feed 复采 224 条：残留实体 0，正常引号 33 条）
16. `NOT NULL: perks.product` 抛异常让**整批**分析中止 —— 而 product 为 NULL 恰恰是反幻觉
    回溯的正确结果；改为逐条拒绝 + `perks_rejected` 计数，一个坏福利杀不掉一轮
17. `run_once` 在分析之前调 `flag_incomplete` → 本轮新建的福利不会被判「缺关键证据」；
    改为分析+核验之后再判一次
18. 日报状态表 `pending | 0` 与 `待核实 | 7` 同时出现（英文键播种、中文键写入）
    → 统一按标签聚合并保持固定顺序

第一次真实调用 DeepSeek（2026-10-15 19:00 后，低谷期）又暴露 5 条，全部只有真 key 才看得见：

20. `deepseek-flash` 默认开启思考，1600 个输出 token **全部**花在推理上
    （`reasoning_tokens=1600`、`finish_reason=length`、`content=""`），看起来像 API 挂了
    → 请求里显式 `thinking: {"type": "disabled"}`；同一篇文章从 5727 token 降到 370，
    并把默认 `DEEPSEEK_MAX_OUTPUT_TOKENS` 提到 4000，截断现在报「被 max_tokens 截断」而不是「空响应」
21. DeepSeek 现在拒绝 `response_format: json_schema`（400 "This response_format type is
    unavailable now"），而这个 400 是 `APIError` 的子类，被 tenacity 重试 3 次再退避 6 秒
    → 4xx 不再重试；首次被拒后本批不再尝试严格模式（一批只付一次 400，而不是每篇一次）
22. 严格模式的兜底路径把 `usage` 丢了，账单显示 `token: {0,0,0}` —— 花了钱却看不见
    → 两条路径统一记录 usage
23. `json_object` 模式没有 schema 约束，模型自造键名（权益写进 `description`、产品写进
    `provider`），真实结果是被证据闸门全部清空 → 22 篇产出 0 条福利。
    修法两步：把**允许的键名逐字写进 system prompt**；再加同义词归位
    （`description→entitlement`、`provider→product`，引文的 `field` 标签一起改）。
    同一批重跑：**10 条福利 / 26 篇**，证据不足被拒 3 条
24. 正文抽取把链接丢了：GitHub Copilot 定价页 HTML 里 42 个链接，抽完只剩 2 个，
    于是 `claim_url` 永远是 null，官方核验对每条福利都无事可做
    → trafilatura 改为 `include_links=True`（txt 输出保留 `[锚文本](url)`），
    同页链接数 2 → 42
25. `DB_PATH=data/bp.db` 是相对路径 → 在项目目录外敲 `bp` 会**静默新建一个空库**（我实测在
    `~` 下跑出「no such table: perks」，并留下一个 4 KB 的空 `~/data/bp.db`）。
    空库不报错，只是什么都没有 —— 对定时器来说同样致命，因为 systemd 的 `WorkingDirectory`
    一旦写错就是这个症状。改为在 Settings 里把相对路径锚定到 `PROJECT_ROOT`，
    绝对路径保持不变（测试全部用绝对临时目录，所以不受影响）
26. 定时器一装上就立刻触发第一轮 tick，会**把整个积压队列一次吃完**（我实测装完 7 秒
    就开始跑，768 篇排队等模型）。与「先小口看看质量」的用法直接冲突。
    → 新增 `ANALYSE_BATCH_LIMIT`（默认 80，0 = 不限）作为单晚送模型上限，
    `run_once` 在未显式传 limit 时用它；手动 `bp analyse` 仍按你的 `--limit` 走

27. 预览日报会踩坑：`report.generate()` 除了渲染还会**写 `reports` 水位行**，
    我用它预览两次，把今天这行的 `generated_at` 推到 21:03 并覆盖掉 `markdown_path`
    → 后果是当晚 20:37 新增的 3 条福利会被明早的日报算成「旧」而漏发。
    已删掉被污染的那行（明早是首封完整日报），并改用 `build_sections` + `render` 做无副作用预览

28. **第一封邮件里大量重复**：六个板块是同一批福利的六个视角，天然重叠。实测 28 条福利
    被渲染了 **59 次**——一条缺链接的新福利同时是「新发现」和「需人工判断」，
    一条来源合并触发的又是「新发现」+「规则更新」。你以为是 59 条福利，其实只有 28 条。
    → `assign_sections()` 按 `SECTION_PRIORITY`（即将截止 › 已过期 › 已确认 › 新发现 ›
    规则更新 › 需人工判断）把每条福利**只归入一个板块**，板块末尾与阅读须知说明折叠了多少条。
    优先级把「已确认」放在「新发现」之前，是为了让 `REPORT_REPEAT_LIVE_LIST=false` 时
    已确认的福利仍然留在 `confirmed_live` 里可被通知到

29. **重复的第二半：跨天重复**。`needs_review` 板块按 `updated_at > 上次水位` 开窗，而
    每 15 分钟一次的 tick 都会跑 `flag_incomplete()`，它把 28 条缺链接的福利**无差别重写一遍**
    （值一模一样，只有 `updated_at` 被推高）。实测 23:46 那次空转 tick 之后，全库 27 行的
    `updated_at` 都变成了同一个时刻。`store.upsert_perk()` 在 `unchanged` 分支上也有同样的问题
    （每晚 500 条重复条目都会命中）。
    → 两处都改成**只在真的变了才写**：重复打标与转载同一套规则不再动 `updated_at`，
    只有置信度真的更高才记录。否则同一条待核实福利会出现在之后每一封日报里。
    → 已被空转推高的那批时间戳无法还原，改为把 10-15 的 `reports` 水位行补回到现在时刻
    （`sent_at` 仍是真实的 20:37 发信时刻），让增量窗口不再覆盖它们

### 需要你人工验证（我无法替你做）

| 事项 | 为什么必须人工 | 怎么验证 |
|---|---|---|
| **新版日报的观感** | 倒计时、按产品分组、跨板块去重都已实现并有测试覆盖（308 条全绿），但「读起来顺不顺眼」只有你知道 | 明早 10:00 那封就是新版；觉得哪块还多余或缺失，说一声改 `src/bp/report.py` + `templates/report.md.j2` / `report.html.j2` |
| **DeepSeek 账单口径对账** | 调用与解析已实跑通（见上一节），但只有你能看到控制台的真实扣费 | 跑 `uv run bp analyse`（低谷期），然后到 platform.deepseek.com 用量页对一下 token 数与 `bp analyse` 打印的是否同一量级；顺便确认低谷价 |
| **抽取质量**（不是格式，格式已测） | 只有真文章 + 真模型才知道抽得对不对 | 跑一晚，第二天 `bp ls --status pending` + `bp show <id>` 对照原文，看有无幻觉、有无漏字段。发现漏抽就往 `config/keywords.toml` 加触发词 |
| **公众号来源** | 第一期按你的决定不碰；需要 RSSHub 或 Mp2RSS 之类的中转 | 想加时：把可用的公众号 RSS 地址 `bp sources add --url ... --name ...` 即可，管道不用改 |
| **明早 10:00 那封是不是定时器自己发的** | `bp-tick.timer` 已装并 active（每 15 分钟一次状态机），夜间采集已实测跑过一轮；但「到点自己发信」这一条还没在无人值守下验证过一次 | 明早收到信即算通过。没收到：`systemctl --user list-timers`、`journalctl --user -u bp-tick.service -n 50` |
| **来源清单长期有效性** | 站点会改改版、改 RSS 结构 | 每周跑一次 `bp sources check`，`bp sources list` 里带 `!` 的就是坏了的 |
| **积压队列的消化速度** | 花多少钱取决于你愿等多久 | 剩 ~700 篇 `pending`（含正文改版需重送的新版本）。定时器每晚只吃 `ANALYSE_BATCH_LIMIT=80` 篇，按实测预筛命中率约一半 → 每晚 ~40 次调用、~15 万 prompt token，一周左右清完。想立刻清完：`bp analyse`（不带 `--limit`） |

---

## 15. 故障排查

| 症状 | 处理 |
|---|---|
| `bp: command not found` | 用 `uv run bp ...`，或 `source .venv/bin/activate` |
| 邮件 `SMTPAuthenticationError` | 99% 是用了账号密码而不是**应用专用密码**；确认两步验证已开 |
| `bp tick` 一直说「非低谷时段」 | 正常，保护账单。想立刻看效果：`bp analyse --force`（会按高峰价计费） |
| `来源清单` 里某个源一直报错 | `bp sources disable <id>`，或把它从 `config/sources.toml` 删掉 |
| 想彻底重来 | 删 `data/bp.db*` 和 `reports/`，再 `bp init`（历史会丢） |
| 日报里「新发现」是空的 | 水位只认**已送达**的邮件（`sent_at`）。只生成没发送时基线不动，属正常；要推进就 `bp report --send` |
| 测试慢 | 见 §14 第 13 条；确认 `tests/conftest.py` 用的是 `journal_mode=MEMORY` |

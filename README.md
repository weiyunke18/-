# 国家中小学智慧教育平台教材下载工具

一个命令行工具，用于从 [国家中小学智慧教育平台](https://basic.smartedu.cn/)（basic.smartedu.cn）批量下载教材 PDF，覆盖小学、初中、高中、特殊教育全学段，共 **3565 册**教材（持续更新）。

- **纯标准库实现**：无需 `pip install` 任何第三方包，Python 3.8+ 即可运行
- **访客登录认证**：自动完成平台访客令牌获取与 `x-nd-auth` 请求签名，令牌自动缓存、过期自动刷新
- **断点续传**：网络中断后重跑同一命令，自动从已下载位置继续（HTTP Range）
- **多线程并发**：默认 4 线程并发下载，多 CDN 主机自动故障转移
- **智能目录结构**：自动按「学段/学科/版本/年级/册次」分类归档

---

## 目录结构

```
smartedu-textbook-downloader/
├── smartedu_dl.py      # 主程序（CLI 入口）
├── des_pure.py         # 纯 Python DES-ECB 实现（认证令牌加解密，零依赖）
├── requirements.txt    # 依赖说明（空依赖）
├── .catalog.json       # 教材目录缓存（首次运行自动生成，约 34MB）
├── .token.json         # 访客令牌缓存（自动生成，过期自动刷新）
└── downloads/          # 默认输出目录
```

---

## 快速开始

```bash
# 1. 查看所有筛选项（学段/学科/版本/年级/册次可选值）
python smartedu_dl.py filters

# 2. 搜索教材
python smartedu_dl.py search 道德与法治

# 3. 按条件列出教材
python smartedu_dl.py list --stage 小学 --subject 道德与法治 --version 统编版

# 4. 下载（默认输出到 downloads/）
python smartedu_dl.py download --stage 小学 --subject 道德与法治 --version 统编版 -y

# 5. 下载单本（--id 支持 contentId 或平台教材详情页 URL）
python smartedu_dl.py download --id bdc00134-465d-454b-a541-dcd0cec4d86e
python smartedu_dl.py download --id "https://basic.smartedu.cn/tchMaterial/bdc00134-465d-454b-a541-dcd0cec4d86e"
```

下载后的文件按如下结构归档：

```
downloads/
└── 小学/
    └── 道德与法治/
        └── 统编版/
            ├── 一年级/
            │   ├── 上册/
            │   │   └── （根据2022年版课程标准修订）义务教育教科书·道德与法治一年级上册.pdf
            │   └── 下册/
            └── 学生读本/
                └── 学生读本（小学低年级）/
```

---

## 命令详解

### 全局参数

```bash
python smartedu_dl.py [-o 输出目录] <子命令>
```

| 参数 | 说明 |
|---|---|
| `-o, --output` | 输出目录，默认 `downloads`。注意放在子命令**之前** |

### `filters` — 查看筛选项

列出目录中全部可选值：学段（6）、学科（37）、版本（114）、年级（21）、册次（180）。

### `list` — 按条件列出教材

```bash
python smartedu_dl.py list [--stage 小学] [--subject 语文] [--version 统编版] \
                           [--grade 一年级] [--term 上册]
```

所有条件可组合，均支持模糊匹配（`--subject 道法` 也能匹配「道德与法治」）。

### `search` — 关键词搜索

```bash
python smartedu_dl.py search 关键词
```

按标题模糊搜索，展示 ID、类型、分类信息，方便复制 ID 去下载。

### `info` — 查看教材详情与 PDF 清单

```bash
python smartedu_dl.py info <contentId 或详情页 URL>
```

列出该教材包含的所有 PDF 文件（含资源包），下载前可先确认。

### `download` — 下载

```bash
python smartedu_dl.py [-o 目录] download [选项]
```

| 选项 | 说明 |
|---|---|
| `--id ID` | 下载单本，ID 可为 contentId 或教材详情页 URL |
| `--stage/--subject/--version/--grade/--term` | 按分类筛选下载，可组合 |
| `--keyword 关键词` | 在筛选结果中再按标题过滤 |
| `--all` | 下载全部教材（约 3565 册，体积巨大，慎用） |
| `--workers N` | 并发下载数，默认 4。网络好可调至 8~16 |
| `-y, --yes` | 跳过下载前的确认提示 |

示例：

```bash
# 下载初中数学人教版全部册次
python smartedu_dl.py download --stage 初中 --subject 数学 --version 人教版 -y

# 高中英语外研社版，只下载必修第一册，8 线程
python smartedu_dl.py -o D:/教材 download --stage 高中 --subject 英语 \
    --version 外研社版 --term "必修 第一册" --workers 8 -y

# 按 ID 下载单本
python smartedu_dl.py download --id bdc00134-465d-454b-a541-dcd0cec4d86e
```

### `refresh` — 刷新目录缓存

```bash
python smartedu_dl.py refresh
```

强制从平台 CDN 拉取最新教材目录（平台更新教材后使用）。日常使用无需手动刷新，缓存会自动校验版本号。

---

## 目录缓存与令牌机制

| 缓存文件 | 作用 | 刷新方式 |
|---|---|---|
| `.catalog.json` | 教材目录（3565 册元数据），避免每次联网拉取 34MB 数据 | 自动按版本号校验；`refresh` 子命令强制刷新 |
| `.token.json` | 访客登录令牌，PDF 下载请求签名用 | 过期后自动重新获取（无需手动操作） |

**删除 `.catalog.json` 或 `.token.json` 不影响使用**，下次运行会自动重新生成。

---

## 常见问题

**Q：下载报 401 Unauthorized？**
工具内置令牌自动刷新，偶尔因平台风控需要重新登录时也会自动处理。若持续失败，删除 `.token.json` 后重试即可强制重新登录。

**Q：下载中断了怎么办？**
直接重跑同一条命令。已下载完整的文件自动跳过，未完成的文件从断点位置继续（基于 HTTP Range），不会重复下载。

**Q：下载速度慢？**
调大并发数：`--workers 8`。平台 CDN 限速与网络环境有关，一般 4~8 线程即可跑满带宽。

**Q：某个文件一直下载失败？**
多为 CDN 瞬时故障。工具已内置多主机（r1/r2/r3）故障转移与重试，仍失败时重跑命令即可（支持断点续传，不会从头下）。

**Q：提示「Cloudflare 418 / nd-trust-zone-check」？**
这是平台对请求来源的风控校验。本工具只走 CDN 公开接口并模拟正常访客行为，一般不会触发；若出现请稍后再试。

**Q：资源包（thematic_course）和普通教材有什么区别？**
普通教材（assets_document，3242 册）每个是一册书的一个 PDF；资源包（323 册）是同一内容Id 下含多个 PDF 的合集，工具会自动逐个提取下载，全部归入同一目录。

**Q：Python 版本要求？**
Python 3.8 及以上。仅用标准库（`urllib`、`hmac`、`hashlib`、`json`、`concurrent.futures` 等），无需安装任何第三方包。

---

## 技术原理（简述）

1. **教材目录**：来自平台 CDN 的静态 JSON 分卷文件（`data_version.json` + `part_*.json`），工具拉取后缓存到本地并按标签维度（学段/学科/版本/年级/册次）解析。
2. **教材详情**：每个 contentId 对应一个详情 JSON，从中提取 PDF 的存储路径。
3. **PDF 地址与鉴权**：PDF 位于私有 CDN（`*-ndr-private.ykt.cbern.com.cn`），请求需带 `x-nd-auth` 头。该头是 HMAC-SHA256 签名，密钥通过访客登录流程获取。
4. **访客登录**：生成 device_id → 创建 session → 获取 guest token（DES-ECB 加密）→ 解密得到 mac_key，全程无账号密码。
5. **DES-ECB**：`des_pure.py` 为纯 Python 实现（已与 pycryptodome 交叉验证 200 组随机数据一致），避免引入第三方依赖。

---

## 使用须知

本工具仅用于个人学习、教学研究与离线查阅等正当用途。平台教材版权归人民教育出版社等各出版单位所有，请勿将下载的 PDF 用于商业传播或再分发。平台资源以官方发布为准，本工具与平台官方无关联。

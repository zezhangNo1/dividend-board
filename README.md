# 高股息看板（Web 版）

A股高股息标的数据看板：118 只红利类股票的股息率、每股分红、四档股息率参考价、近一年位置与估值。纯静态站，无后端、无账号、无追踪。

> 数据来自公开行情接口，仅供参考，不构成任何投资建议。

## 目录结构

```
web/
├── index.html       看板主页（桌面表格视图 + 移动端卡片视图）
├── about.html       数据口径说明与免责声明
├── site_data.js     数据快照（分红 + 板块归类，页面直接 <script> 引入）
├── universe.json    标的池 {代码: 板块} 映射，单独 clone 也能跑数据更新
├── tools/
│   └── fetch_data.py  数据更新脚本（行情走腾讯、分红走东财，输出 site_data.js）
└── README.md
```

## 本地预览

```bash
cd web
python3 -m http.server 8848
# 打开 http://127.0.0.1:8848/
```

直接双击 index.html（file:// 协议）也能打开：分红数据在 site_data.js 里，盘中行情由浏览器直接向腾讯行情接口获取，不需要本地服务。

## 更新数据

```bash
python3 tools/fetch_data.py
```

- 分红数据建议按季度跑一次；行情不需要跑脚本，页面打开时实时取。
- 标的来源：`universe.json` 为准；若本目录位于原内容项目（xianyu）之内，脚本会顺带扫描上级目录的卡片文件，把新发布的标的自动加进来并回写 `universe.json`。
- 依赖：Python 3.8+、curl。无第三方包。

## 部署（GitHub Pages）

1. 推送到 GitHub 仓库。
2. 仓库 Settings → Pages → Source 选 `main` 分支 `/ (root)`。
3. 访问 `https://<用户名>.github.io/<仓库名>/`。

也可以部署到任意静态托管（对象存储、Cloudflare Pages 等），无需服务端。

## 口径速查

- **每股分红 DPS**：最近一个「年度分红已实施」财年的（中期 + 年度）合计。
- **股息率** = DPS ÷ 现价。
- **参考价** = DPS ÷ 目标股息率（5.5% / 5.0% / 4.5% / 4.0% 四档，纯算术换算，非买卖建议）。
- **近一年位置** = (现价 − 52周最低) ÷ (52周最高 − 52周最低)。

完整口径见 [about.html](about.html)。

# A股权益超级基金止盈监控网页：稳定数据版

这版不再依赖浏览器直接逐只抓天天基金 / 东方财富 / 新浪 / 腾讯接口。

## 为什么要这样改

GitHub Pages 是纯静态网页，浏览器直接访问第三方金融接口会遇到：

- CORS 跨域限制；
- HTTPS 页面调用 HTTP 接口被浏览器阻止；
- JSONP 回调名冲突；
- 第三方接口偶发限流或单只基金失败；
- 新浪、腾讯部分接口在 HTTPS 环境下不可用或超时。

所以稳定方案是：

> GitHub Actions 在云端定时运行 Python 脚本抓净值 → 生成 `data/fund_latest.json` 和 `data/fund_history.json` → 静态网页只读取自己仓库里的 JSON。

这样网页展示时不再直接依赖第三方接口，稳定性会高很多。

## 文件结构

把整个压缩包解压后上传到 GitHub 仓库根目录：

```text
index.html
scripts/update_fund_data.py
data/fund_latest.json
data/fund_history.json
.github/workflows/update-fund-data.yml
```

## 第一次部署

1. 把上述文件上传到 GitHub Pages 仓库根目录。
2. 进入仓库 Settings → Pages，确认 Pages 发布分支是 `main`，目录是 `/root`。
3. 进入仓库 Actions 页面，打开 `Update fund NAV data`。
4. 点击 `Run workflow` 手动运行一次。
5. 运行成功后，仓库里的 `data/fund_latest.json` 和 `data/fund_history.json` 会自动更新。
6. 打开网页，按 `Ctrl + F5` 强制刷新。

## 日常更新

工作流默认在北京时间周一到周五 22:30 运行一次。这个时间适合公募基金正式净值披露后更新。

如果某天想手动更新：

1. 进入 GitHub 仓库 Actions；
2. 选择 `Update fund NAV data`；
3. 点击 `Run workflow`。

## 数据源顺序

Python 脚本的数据源顺序：

1. AKShare：`fund_open_fund_info_em(symbol=基金代码, indicator="单位净值走势")`
2. 东方财富 F10 历史净值接口
3. 天天基金 `fundgz` 最新净值接口
4. 如果某只基金当次失败，沿用上一次成功的旧 JSON 数据，避免网页显示空白

## 注意

- `index.html` 仍保留浏览器直连接口作为兜底，但优先使用本地 JSON。
- 如果 GitHub Actions 没有运行成功，网页会继续使用旧 JSON 或内置基准。
- 这套方案仍然可以放在 GitHub Pages，不需要服务器。

## 2026-05-12 修正：正式净值与估算净值

天天基金 fundgz 接口同时包含 `dwjz/jzrq`（正式单位净值/净值日期）和 `gsz/gztime`（盘中估算净值/估算时间）。
本版本默认“确认净值优先”只采用 `dwjz/jzrq`；只有在网页选择“盘中估算优先”时才采用 `gsz/gztime`。
后端脚本也会用正式历史净值重新计算日涨跌，避免估算涨跌幅混入正式净值。

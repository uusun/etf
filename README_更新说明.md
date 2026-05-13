# 超级基金监控网页 v8

本版只修后端净值源分层：

- 晚间正式净值：东方财富 JSON / 东方财富 F10 / AKShare / 新浪 of_ / 天天 dwjz。
- 盘中估算参考：天天 gsz/gztime、 新浪 fu_。
- 同花顺与腾讯不再进入抓取主链路，避免无效错误噪音。
- 前端盘中估算功能保留，不影响白天使用。

替换文件：

- `index.html`
- `scripts/update_fund_data.py`
- `.github/workflows/update-fund-data.yml`

手动运行 Actions 后，日志第一行应显示：

`SCRIPT_VERSION=v8_official_plus_sinaof_20260513`

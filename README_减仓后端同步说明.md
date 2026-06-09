# A股超级基金止盈监控 v12 - 减仓记录后端同步版

## 文件说明
- `index.html`：网页。
- `scripts/update_fund_data.py`：GitHub Actions 抓取净值。
- `.github/workflows/update-fund-data.yml`：每天自动更新净值。
- `data/reduction_records.json`：减仓记录“数据库”。

## 减仓记录同步
网页新增“减仓记录 / 后端同步”模块。新增记录后，点击“同步到GitHub”，网页会通过 GitHub Contents API 把记录写入 `data/reduction_records.json`。

需要 GitHub fine-grained token：只给当前仓库 `Contents: Read and write` 权限。Token 只填写在网页里，不要写入源码。

## 15%等比例减仓
点击“生成15%草稿”会按当前剩余份额生成 14 条记录。建议实际成交后核对每只基金的成交净值、份额和到账金额，再同步到 GitHub。

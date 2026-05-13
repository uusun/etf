# 超级基金净值监控稳定版 v6

## 本版重点

- `SCRIPT_VERSION=v6_multi_source_20260513`
- 后端最新净值候选源：东方财富 JSON API、东方财富 F10、AKShare、同花顺爱基金、天天基金 fundgz、新浪、腾讯。
- 历史净值优先源：东方财富 JSON API；备用：东方财富 F10、AKShare。
- 前端日常建议使用：`自动：本地JSON + 前端补采`。
- 新浪/腾讯前端直连不稳定，已作为后端备用源纳入 Python 脚本。

## 替换文件

覆盖仓库中的：

```text
index.html
scripts/update_fund_data.py
.github/workflows/update-fund-data.yml
```

然后手动运行一次 GitHub Actions：`Update fund NAV data`。

## 正常日志示例

```text
SCRIPT_VERSION=v6_multi_source_20260513
Fetching 014362 ...
  OK 2026-05-13 1.2222 via 东方财富JSON最新净值 date_ok=True
  candidates: 东方财富JSON最新净值:2026-05-13:1.2222 | 东方财富F10最新净值:2026-05-13:1.2222 | ...
```

# 超级基金止盈监控网页 v5_f10_primary

本版只修净值更新后端，不重构网页。

## 替换文件

请覆盖仓库中的：

- `index.html`
- `scripts/update_fund_data.py`
- `.github/workflows/update-fund-data.yml`

## 验证方式

手动运行 GitHub Actions 后，在日志开头必须看到：

```text
SCRIPT_VERSION=v5_f10_primary_20260513
```

每只基金下面会打印候选源，例如：

```text
candidates: 东方财富F10最新净值:2026-05-13:1.2222 | AKShare fund_open_fund_info_em:2026-05-12:1.2209 | 天天基金fundgz确认净值:2026-05-12:1.2209
```

如果看不到 `SCRIPT_VERSION=v5_f10_primary_20260513`，说明 GitHub 实际运行的仍然是旧脚本。

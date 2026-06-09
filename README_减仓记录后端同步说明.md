# v13 减仓记录后端同步版

页面版本：`v13_reduction_backend_manual_20260609`

## 覆盖文件

将压缩包解压后，把以下文件覆盖到 GitHub 仓库根目录：

- `index.html`
- `scripts/update_fund_data.py`
- `.github/workflows/update-fund-data.yml`
- `data/reduction_records.json`

如果仓库里已经存在 `data/reduction_records.json` 且里面已有正式减仓记录，请不要覆盖它；只覆盖 `index.html` 即可。

## 减仓记录逻辑

网页会自动读取：

```text
data/reduction_records.json
```

手动新增记录后，先保存在当前浏览器本地。点击“同步到 GitHub”后，会通过 GitHub API 写回仓库里的 `data/reduction_records.json`。

同步需要 GitHub fine-grained token，只给当前仓库：

```text
Contents: Read and write
```

Token 只保存在当前浏览器 `sessionStorage`，不会写入 `index.html` 或仓库。

## 已移除

- 已移除“生成 15% 草稿”按钮。
- 只保留手动录入减仓记录。

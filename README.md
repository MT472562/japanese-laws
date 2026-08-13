# 日本の現行法令 Git アーカイブ

e-Gov法令API Version 2を正本として、日本の現行法令を法令ごとのMarkdownに変換し、Gitの差分として追跡するためのリポジトリです。

> [!IMPORTANT]
> このリポジトリは閲覧・差分確認用の複製です。法的な確認には必ず[e-Gov法令検索](https://laws.e-gov.go.jp/)の原文を利用してください。

## 仕組み

- e-Gov公式の全法令一括ZIPを取得し、毎回の現行スナップショットを再構築
- 法令ごとに `laws/<法令種別>/<法令ID>.md` として保存
- 現行一覧から消えた法令のファイルを削除（廃止をGit差分で表現）
- `INDEX.md` と `manifest.json` を決定的に再生成
- GitHub Actionsで毎日自動同期し、変更がある日だけコミット

## ローカル同期

Python 3.11以降だけで動作し、外部パッケージは不要です。

```bash
python scripts/sync_laws.py --full
```

約316MB（サイズは更新により変動）の公式一括ZIPを1回取得し、ローカルで約9,000件のMarkdownへ変換します。法令ごとの個別取得は行いません。途中で失敗した場合、既存の法令ファイルとマニフェストは更新されません。再実行してください。

少数で動作確認する場合：

```bash
python scripts/sync_laws.py --limit 10 --no-prune
```

`--limit` 使用時はAPIによる少数取得になります。不完全な一覧であるため、削除処理は自動的に無効になります。

すでに公式一括ZIPをダウンロードしている場合は、再ダウンロードせずに変換できます。

```bash
python scripts/sync_laws.py --bulk-archive all_xml.zip
```

## 自動更新

`.github/workflows/sync.yml` は毎日03:17 JST（18:17 UTC）に公式の全法令ZIPを取得し、現行スナップショットを再構築します。法令ごとのAPIアクセスは行いません。GitHub側で Actions の `Read and write permissions` が許可されていれば、変更を自動コミットします。手動実行にも対応しています。

全件を照合し直す場合：

```bash
python scripts/sync_laws.py --full
```

## ファイル構成

```text
laws/<law_type>/<law_id>.md  法令本文
INDEX.md                     法令種別ごとの索引
manifest.json                現在の法令ID・版ID対応表
scripts/sync_laws.py         同期・Markdown変換プログラム
tests/                       変換処理のテスト
```

法令ファイル名には法令名ではなく安定した法令IDを使います。改称や記号を含む名称でもパスが変化せず、履歴を追いやすくするためです。

## 出典・利用条件

- データ出典：[e-Gov法令検索](https://laws.e-gov.go.jp/)
- API仕様：[法令API Version 2](https://laws.e-gov.go.jp/api/2/swagger-ui)
- e-Gov掲載データの利用：[e-Govコンテンツ利用規約](https://www.e-gov.go.jp/terms)

取得元、取得方法、変換プログラムを明示し、機械的な変換結果と独自編集を混在させない設計です。

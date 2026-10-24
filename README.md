# ポケット mycmux サーバー

iPhoneからmycmuxの作業状況を確認し、会話や操作を手元のPCへ渡すサーバーとPWAです。PythonのAPIサーバーと `web/` の画面を含みます。iPhoneのネイティブアプリは別プロジェクトです。

**開発者向けのソースプレビューです。** 閲覧専用ではなく、ターミナルへの入力、セッションの起動や終了、ファイルの取得、アカウント切替など、PC利用者の権限で実際の操作ができます。日常環境への導入前に、隔離した状態フォルダと架空データで確認してください。

English: A local server and PWA for viewing and interacting with mycmux sessions from an iPhone over a trusted private network. This is a developer source preview, not a public Internet service. Mail and research integrations are unavailable in this distribution.

## 必要な環境

Python 3.12、利用者自身のmycmux、Tailscaleを想定しています。Python標準ライブラリを中心に構成していますが、Windowsでは `zoneinfo` 用の時刻データが必要です。`ZoneInfoNotFoundError` が出る環境にはPythonの `tzdata` パッケージを別途用意してください。テストにはpytestが必要です。

mycmuxの制御APIと認証を利用できることが前提です。Windows用のversioned CLIを同梱し、macOSでは `~/mycmux/scripts/mycmux_agent_cli.py` を既定とします。場所が異なる場合は後述の `bridgeCli` を設定します。

## 設定してから起動する

状態データをソースと分けます。`--state-dir` または `POCKET_STATE_DIR` で専用フォルダを指定し、その中の `machine.json` に利用者自身の設定を書きます。次は架空の例です。URLとフォルダを実環境に合わせ、許可範囲を必要最小限にしてください。

```json
{
  "publicOrigin": "https://your-machine.example.test",
  "allowedFileRoots": ["C:/Users/example/Projects"],
  "launchCwdRoot": "C:/Users/example/Projects",
  "launchPresets": ["C:/Users/example/Projects"],
  "features": {"mail": false, "research": false}
}
```

`bridgeCli` はCLIの絶対パスです。`hubOrigin` はネイティブiPhoneアプリへ渡す任意のHTMLハブのoriginです。PWAのハブ画面はこの設定を使わず、PWAと同じoriginの `/mobile/` を使います。ハブ自体は同梱していません。未設定のハブに作者の接続先は使いません。`.example.test` の例をそのまま接続先にしないでください。

```powershell
python server.py --state-dir "C:/Users/example/PocketState" --port 8994
```

サーバーは `127.0.0.1` にbindします。Tailscale Serve等の信頼する入口で利用者の身元を確認し、mycmux側のPocketペアリングと許可ログイン設定を使ってください。HTTPヘッダーに任意の身元を書いて認証を迂回する構成は使えません。公開インターネット、Funnel、認証なしreverse proxyでの利用は想定していません。

終了は起動端末でCtrl+Cです。既存の状態フォルダを試験に流用しないでください。状態フォルダには端末登録、会話の予約、アップロード、操作ログ等が入るので、Gitへ含めません。

## 公開版で外した連携

メールとリサーチの業務向け文面・外部スクリプトは同梱していません。この2機能は既定で無効です。`true` を指定するとサービス生成前にエラーとなり、保存済みメール・リサーチジョブを読み込んだり再開したりしません。関連実装は拡張の参考として残していますが、この配布版では利用できません。

質問画面の補助bridgeは `POCKET_ASK_BRIDGE_DIR` で明示した場合だけ読み込みます。未設定では無効です。任意のHermes接続には別のサーバーと明示設定が必要です。私用のスキル、ルール、業務資料は同梱しません。スキル一覧や会話表示は、実際の利用時に利用者自身のローカルデータを読む機能です。

`relay_server.py` と `relay_mirror.py` は任意の補助サーバーです。作者端末への接続はありません。使う場合は `POCKET_RELAY_ORIGIN`、`POCKET_WIN_HEALTH_URL`、`POCKET_WIN_PEER_HOST`、`POCKET_WIN_API_BASE` を利用者自身で指定します。URLには認証情報を埋め込まず、信頼できるprivate networkのHTTPS先だけを使ってください。Mac状態表示の対象は `POCKET_MAC_PEER_HOST` で指定します。未設定のpeer確認とmirrorは外部へ接続しません。新規relay環境での動作は未検証です。

## 検証

公開用の既定設定とサービス生成を、実サーバーのimportを避けた単体テストで確認できます。

```powershell
python -m pytest --noconftest -p no:cacheprovider tests/ -q
node tests/test_public_hub.mjs
```

公開準備時にはこれらの試験に加え、人工のhome・Tailscale応答、実通信と実プロセス起動の遮断下で、loopbackの空きポートへの起動と終了を確認しました。メール・リサーチサービスが生成されないことも確認しています。これは全API、認証、CSRF、実機ペアリング、アカウント切替の完全な監査ではありません。

## ライセンスと報告

自作部分は [MIT](LICENSE)。同梱のmycmux CLIはGPL-3.0の別部品で、MITへ変更していません。配布物全体をMIT-onlyと扱わず、[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) と同梱GPL本文を確認してください。

脆弱性や実ログを公開Issueへ書かず、[SECURITY.md](SECURITY.md) に従ってください。変更提案は [CONTRIBUTING.md](CONTRIBUTING.md) を参照してください。

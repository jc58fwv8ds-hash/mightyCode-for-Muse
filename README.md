# mightyCode for Muse

本物の Claude Code CLI を、Mac 上のローカル LLM（MLX）につないで動かすための macOS アプリです。
画面から本人が直接操作することも、Muse（別の AI エージェント）が HTTP API 経由で操作することもできます。

名前は [shi3z/mightyCode](https://github.com/shi3z/mightyCode) への敬意として付けました。
Claude Code をローカルモデルにつなぐという発想を借りていますが、mightyCode のコードは使っていません。

## しくみ

```
ブラウザの画面 (8471) ── backend/server.py ── Claude Code CLI (stream-json)
                                                    │
                                          backend/proxy.py (18081)   Anthropic 形式 → OpenAI 形式
                                                    │
                                          mlx_lm.server (18080)      ローカルモデル
```

- `backend/server.py` 画面と API を提供します（標準ライブラリのみ）。
- `backend/agent.py` Claude Code CLI を起動し、やり取りを中継します。
- `backend/proxy.py` Claude Code の要求をローカルモデル向けに変換します。
- `backend/guard.py` 危険なコマンドを承認の前に止めます。
- `backend/approvals.py` 承認待ちをファイルでも受け渡します（Muse 用）。
- `mac/bootstrap.py` 初回セットアップと各サーバーの起動を行います。
- `frontend/` 画面（素の HTML / CSS / JavaScript）です。

## 必要なもの

- Apple シリコンの Mac（macOS 12 以降）
- [Claude Code CLI](https://claude.com/claude-code)（`~/.local/bin/claude`、`PATH` 上の `claude`、または環境変数 `CLAUDE_BIN` で指定）
- 初回セットアップ用のネット接続と、モデルを置くディスク空き容量

## 使い方

1. `mightyCode for Muse.app` を `/Applications` に置きます。
2. 署名します。リポジトリには署名を含めていません。

   ```bash
   codesign --force --deep -s - "/Applications/mightyCode for Muse.app"
   ```

3. アプリを開きます。初回は `uv`、Python 環境、`mlx-lm`、モデル
   （`shi3z/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic-MLX-4bit`）を
   `~/Library/Application Support/LocalCode/` に用意します。数分以上かかります。
4. ブラウザで `http://127.0.0.1:8471/` が開きます。作業フォルダを入れて「セッション開始」を押します。

### フォルダの許可について

作業フォルダがデスクトップ・書類・ダウンロード・iCloud Drive・外部ボリュームにある場合、
macOS がアクセス許可のダイアログを出します。「許可」を押すまでセッションは待機します。
アプリを編集して署名し直すと、macOS はもう一度許可を求めます。

## Muse から操作する

画面と同じ API を使います。Muse からの操作には `"by": "muse"` を付けると、画面に「Muse」と表示されます。

| 操作 | API |
| --- | --- |
| セッション一覧 | `GET /api/sessions` |
| セッション開始 | `POST /api/session` `{"cwd": "...", "by": "muse"}` |
| 指示を送る | `POST /api/chat` `{"session": "...", "message": "...", "by": "muse"}` |
| 経過を読む | `GET /api/events?session=...`（SSE） |
| 承認・却下 | `POST /api/approve` |
| 停止 | `POST /api/stop` |

詳しい書式は、起動時に `~/Library/Application Support/LocalCode/approvals/README.md` に書き出されます。
macOS のフォルダ許可ダイアログだけは、本人が押す必要があります。

## 補足

- `Contents/MacOS/LocalCode` は `Resources/launcher.sh` を実行するだけの小さな実行ファイルです。ソースはこのリポジトリにありません。
- 旧名は LocalCode です。データフォルダ名、バンドル ID（`jp.muse.localcode`）、環境変数（`LOCALCODE_*`）は旧名のままです。
- ポートは 8471（画面）、8472（セットアップ画面）、18081（プロキシ）、18080（モデル）を使います。

## クレジット

- 発想の元: [shi3z/mightyCode](https://github.com/shi3z/mightyCode)
- 画面の見直しには、Damir Sokolovsky 氏（[@heydamir](https://x.com/heydamir)）の [unslop](https://github.com/heydamir/unslop)（CC BY 4.0）の判定基準を参考にしました。

## ライセンス

未設定です。

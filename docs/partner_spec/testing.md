# テスト計画（実装完了判定 / DoD）

## 単体テスト

### Scheduler

- intent別に MemoryPack の構成が変わる
- token budget 超過時に圧縮が働く

### DB

- upsert（entities/edges/facts/loops）が冪等
- `unit_versions` が正しく積み上がる（payload hash 変更時のみ version+1）

### vec_units

- upsert → KNN → JOIN で本文取得できる（`k` パラメータ方式）

## 統合テスト（LLMスタブ）

- LLM/Embedding API をスタブし、固定JSONを返してパイプラインが成立する

## 回帰テスト（会話継続性）

- 週次サマリ生成後、次週の雑談でサマリが注入される
- open loop が次回に提示され、完了で閉じる

## 受け入れ条件（Definition of Done）

- sqlite-vec を使った KNN取得→JOIN→注入が動く（kパラメータ方式）
- 1回の `/api/chat` で
  - SSEが返る
  - episodeが保存される（`units(kind=EPISODE)+payload_episode`）
  - workerが派生（reflection/entities/facts/loops/embedding）を追加できる
- 週次summaryが生成され、次回会話で注入される
- persona/contractが常時注入され、人格が崩れにくい

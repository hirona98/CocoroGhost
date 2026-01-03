# リマインダー（Reminders）仕様（クライアント向け）

このドキュメントは、クライアント担当が **リマインダー機能**を実装するための仕様書です。
サーバ側は「時間になったことを自分で把握して、人格としてスピーカーへ伝える」体裁を満たすため、
リマインダー発火時に **MemoryPack を使って自然な文面を生成**し、`/api/events/stream` へ push します。

## 用語

- **client_id**: クライアントの安定ID。`/api/chat` の `client_id` と、`/api/events/stream` の `hello.client_id` は一致させる。
- **target_client_id**: リマインダー配信先の client_id。サーバ側で **グローバルに1つ**だけ保持する。
- **time_zone**: IANA timezone（例: `Asia/Tokyo`）。必須。
- **HH:MM**: リマインダー文面で「読み上げる時刻」表現（例: `09:30`）。秒は使わない。

## 要件（サーバ挙動）

- リマインダーは **単発（once）/毎日（daily）/毎週（weekly）** をサポートする。
- weekly は **複数曜日**を指定できる（週の開始は日曜）。
- 予定時刻を過ぎていた場合も **遅れて即発火**する。
- `reminders_enabled` が OFF→ON になった直後は、過去分の発火は **捨てる**（単発は削除、繰り返しは次回を未来に再計算）。
- リマインダーのメッセージは **50文字目安**で生成し、時刻は **HH:MM を読む**。
- クライアントが未接続の場合、サーバは **接続まで待って**から配信する。
- 同時刻に複数dueがある場合は **時刻順に1件ずつ**配信する（まとめ読みしない）。
- 保留キューは **無制限**（ただし実装上はメモリに保持する前提）。
- リマインダー編集により `next_fire_at` が過去になった場合、編集由来の過去分は **スキップして次回へ進める**。

## WebSocket（受信）: `/api/events/stream`

### 接続時の必須手順

クライアントは接続直後に `hello` を送信し、自分の `client_id` をサーバへ登録する。
（リマインダーは target_client_id 宛ての **宛先配信**のため、`hello` が無いと受け取れない）

```json
{
  "type": "hello",
  "client_id": "console-uuid-or-stable-id",
  "caps": []
}
```

### 受信イベント: `type="reminder"`

```json
{
  "unit_id": 12345,
  "type": "reminder",
  "data": {
    "reminder_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "hhmm": "09:30",
    "message": "AI人格のセリフ（50文字目安、時刻HH:MMを含む）"
  }
}
```

補足:
- `reminder` はリアルタイム性が本質のため、サーバ側で **バッファしない**（再接続時のキャッチアップ対象外）。
- `unit_id` は `memory_enabled=true` の場合のみ有効なEpisodeのID（無効時は `0` または `-1` になりうる）。

## REST API: `/api/reminders`（UI用）

リマインダーは `/api/settings` ではなく **専用API**で管理する（全置換を避けるため）。

### 1) グローバル設定

#### `GET /api/reminders/settings`

```json
{
  "reminders_enabled": true,
  "target_client_id": "console-uuid-or-stable-id"
}
```

#### `PUT /api/reminders/settings`

```json
{
  "reminders_enabled": true,
  "target_client_id": "console-uuid-or-stable-id"
}
```

### 2) リマインダー一覧

#### `GET /api/reminders`

```json
{
  "items": [
    {
      "id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "enabled": true,
      "repeat_kind": "once|daily|weekly",
      "content": "string",
      "scheduled_at": "2026-01-03T09:30:00+09:00",
      "time_of_day": "09:30",
      "weekdays": ["sun","mon"],
      "next_fire_at_utc": 1767400200
    }
  ]
}
```

補足:
- `scheduled_at` は `repeat_kind=once` のときのみ有効。
- `time_of_day` / `weekdays` は `daily/weekly` 用。
- `next_fire_at_utc` はサーバ管理フィールド（UIは表示するだけでよい）。

### 3) 作成

#### `POST /api/reminders`

- once:
```json
{
  "time_zone": "Asia/Tokyo",
  "enabled": true,
  "repeat_kind": "once",
  "scheduled_at": "2026-01-03T09:30:00+09:00",
  "content": "string"
}
```

- daily:
```json
{
  "time_zone": "Asia/Tokyo",
  "enabled": true,
  "repeat_kind": "daily",
  "time_of_day": "09:30",
  "content": "string"
}
```

- weekly:
```json
{
  "time_zone": "Asia/Tokyo",
  "enabled": true,
  "repeat_kind": "weekly",
  "time_of_day": "09:30",
  "weekdays": ["sun","wed","fri"],
  "content": "string"
}
```

### 4) 更新

#### `PATCH /api/reminders/{id}`

更新できる項目:
- `enabled`
- `content`
- `time_zone`
- `repeat_kind`
- `scheduled_at`（once）
- `time_of_day`（daily/weekly）
- `weekdays`（weekly）

注意:
- 編集により `next_fire_at` が過去になった場合は「編集由来の過去分」は捨て、次回を未来に再計算する。

### 5) 削除

#### `DELETE /api/reminders/{id}`

## UI要件（クライアント実装）

- 画面: 「リマインダー」専用ページを持つ（settings画面とは分離してよい）。
- グローバル設定:
  - `reminders_enabled` トグル
  - `target_client_id`（基本は自分の client_id をセットする。複数端末運用は将来）
- リマインダー編集:
  - 種別: 単発 / 毎日 / 毎週
  - 単発: 日付 + 時刻（time_zoneに基づく）
  - 毎日: 時刻（HH:MM）
  - 毎週: 曜日複数選択 + 時刻（HH:MM）
  - 内容（content、必須）
  - 有効/無効（enabled）
- 受信（スピーカー）:
  - `type="reminder"` を受信したら、即時にスピーカー出力（テキスト表示＋音声）。
  - 連続で複数来る可能性があるため、クライアント側でキューイングして順に再生する。

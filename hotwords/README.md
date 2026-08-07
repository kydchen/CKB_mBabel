# Hotwords

Domain vocabulary for the ASR side, curated for the Nervos CKB blockchain ecosystem (mined from community Telegram chats and the Nervos Talk forum, then hand-filtered: rare proper nouns that ASR garbles get priority, common words are excluded).

- `hotwords.txt` — English entries, one per line.
- `hotwords_zh.txt` — Chinese entries.
- `boosting_table.txt` — generated on every app start from the merged lists (platform rules: <10 chars per entry, no punctuation, digits spelled as Chinese characters, e.g. `P2P` -> `P二P`). Upload it in the speech console 自学习平台 → 热词管理, then set `VOLC_BOOSTING_TABLE_ID` in `.env`. Direct hotword transmission caps at 100 tokens; the table lifts that to thousands.

Replace these lists with your own domain's vocabulary: company and project names, people names, technical terms. The app reloads them on every start.

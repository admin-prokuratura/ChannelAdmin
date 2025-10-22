# Channel Admin Bot

This repository contains the core domain logic for a Telegram bot that manages a paid posting system for a channel and a linked chat.  

## Features

- **Energy economy** – virtual currency that users spend to publish posts in the channel and linked chat.
- **Golden cards** – premium currency that pins purchased posts for a configurable amount of time.
- **Word filtering** – configurable list of banned words that prevents inappropriate or unwanted content from being submitted.
- **Referral rewards** – grant extra energy to referrers when invited friends register.

The project focuses on the domain logic so it can be tested independently from Telegram.  A light-weight wrapper around `python-telegram-bot` is provided to demonstrate how the services can be wired into a real bot.

## Getting started

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Running the sample bot

```bash
export TELEGRAM_BOT_TOKEN="<your-token>"
export CRYPTOPAY_TOKEN="<crypto-pay-api-token>"  # enables inline payments
python -m channel_admin.bot
```

The sample bot now guides the user through an emoji-rich inline menu. All core actions are available as buttons, while legacy
slash commands remain for compatibility:

- `/start` – register a new user after subscribing to sponsors (grants 100 energy once).
- `/balance` – show the current amount of energy and golden cards.
- `/buy_energy <amount>` – purchase additional energy according to the configured price table.
- `/buy_golden_card <duration>` – purchase a golden card that pins the next post for `duration` hours.
- `/post <message>` – submit a post that will be sent to the channel and chat if the word filter approves it.

The sample implementation uses in-memory storage for demonstration purposes.  Integrate it with your own persistent storage before deploying to production.

## Tests

```bash
PYTHONPATH=src pytest
```

## Configuration

Key configuration lives in `channel_admin/config.py`:

- `PricingConfig` – defines prices for energy bundles and golden cards.
- `FilterConfig` – sets the banned words list used by the filter.

Both can be overridden or extended depending on the needs of your channel.

## License

MIT

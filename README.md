# Delete Moderation Bot

This bot deletes messages in the target group for users who:
- are not admins in `autopublicbot` database,
- and do not have an ACTIVE campaign in `drafts`.

This bot does not delete messages for:
- users present in `admins`,
- users who have ACTIVE campaign rows (`drafts.status = 'ACTIVE'`).

Data source: `autopublicbot` SQLite (`bot.db`) in read-only mode.

## Environment Variables

- `BOT_TOKEN` (required): token of this delete bot.
- `AUTOPUBLIC_DB_PATH`: path to `autopublicbot` database inside container. Default: `/autopublic-data/bot.db`.
- `TARGET_GROUP_ID`: target group id. If not set, bot reads `settings.target_group_id` from DB.
- `POLL_TIMEOUT_SEC`: long polling timeout. Default: `2`.
- `REQUEST_TIMEOUT_SEC`: Telegram HTTP request timeout. Default: `10`.
- `GET_UPDATES_LIMIT`: getUpdates limit. Default: `100`.
- `MAX_DELETE_PER_CYCLE`: max deletions per loop. Default: `30`.
- `IGNORE_BOT_MESSAGES`: ignore bot-authored messages (`true`/`false`). Default: `true`.
- `SKIP_PENDING_UPDATES_ON_START`: skip old updates on startup. Default: `true`.
- `LOG_LEVEL`: log level (`INFO`, `DEBUG`, etc). Default: `INFO`.

## Docker Run

Example for `/home/dmitry/autopublicbot/data/bot.db`:

```bash
cd ~/delete

docker build -t delete-bot .
docker rm -f delete-bot 2>/dev/null || true

docker run -d \
  --name delete-bot \
  --restart unless-stopped \
  --network host \
  -e BOT_TOKEN='PUT_NEW_DELETE_BOT_TOKEN_HERE' \
  -e AUTOPUBLIC_DB_PATH='/autopublic-data/bot.db' \
  -v /home/dmitry/autopublicbot/data:/autopublic-data:ro \
  delete-bot
```

## Notes

- Add this bot to the group as admin with `Delete messages` permission.
- In BotFather, set `/setprivacy -> Disable` for this bot.
- If `group_moderation_enabled=false` in `autopublicbot` settings, this bot also stops deleting messages.

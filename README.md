# InstaGet — Instagram Downloader

Flask + yt-dlp backend and web app for downloading Instagram videos, Reels, and images.

**Live:** https://sunny-creation-production-05bc.up.railway.app

Also powers the [InstaGet browser extension](https://github.com/khaledq84ever/instagram-extension) — get it from [GetPack](https://getpack-production.up.railway.app).

## API
- `POST /info` `{url}` → title, thumbnail, is_video
- `POST /start` `{url, format}` → `{job_id}`
- `GET /status/<job_id>` → progress / done / error
- `GET /download/<job_id>/<filename>` → file

Deploy: `railway up --ci` from this folder.

# JobPilot landing page

The public page for the JobPilot beta: static files, no build step.

| File | What it is |
|---|---|
| `index.html` | Landing page. Inline CSS, one tiny inline script, no external requests. |
| `privacy.html` | Plain-language privacy page in the same style. |
| `assets/sample-cv.png` | Sample CV rendered by `agent/documents.py` from the fictional `profile.example.yaml`. |
| `tools/sample_cv.py` | Regenerates `assets/sample-cv.png`. Not served (see the nginx block). |
| `assets/og-image.png` | 1200×630 link-preview image (`og:image`) used by both pages. |
| `tools/og-image.html`, `tools/og_image.sh` | Source for `assets/og-image.png` and the headless-Chrome script that renders it. Not served. |

## Renaming the bot

The Telegram bot username is the `data-bot` attribute on the `<html>` tag of `index.html` and `privacy.html`.
Every Telegram link (`https://t.me/<bot>?start=request` for invites) is built from it. To rename:

```bash
sed -i 's/data-bot="job_agent_nilish_bot"/data-bot="NEW_BOT_USERNAME"/' site/index.html site/privacy.html
```

## Preview locally

```bash
python3 -m http.server 8000 --directory site
```

Then open <http://localhost:8000>.

## Regenerate the sample CV

From the repo root:

```bash
pip install -e . pypdfium2 pillow
python site/tools/sample_cv.py
```

It only ever uses `profile.example.yaml` ("Alex Example"). Never point it at a real profile.

## Regenerate the link-preview image

Edit `site/tools/og-image.html` (a fixed 1200×630 page), then from the repo root:

```bash
./site/tools/og_image.sh
```

It uses headless Google Chrome (set `CHROME=/path/to/chrome` if it isn't at the macOS default path). After
deploying, re-scrape the URL in a link-preview debugger such as LinkedIn's Post Inspector, because platforms cache
previews.

## GitHub Pages (live now)

`.github/workflows/pages.yml` publishes the page and `assets/` to <https://nilish1994.github.io/job-match-agent/>
on every push to `main` that touches `site/`. It rewrites the custom domain in the link-preview tags to the Pages
address so previews work there. `tools/` and this README aren't published.

## Serving with nginx

Copy `site/` to the server (for example `/srv/jobpilot/site`), then add a server block. certbot adds the
`listen 443 ssl` lines and certificates when you run `certbot --nginx -d jobpilot.imisstech.com.au`.

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name jobpilot.imisstech.com.au;

    root /srv/jobpilot/site;
    index index.html;

    # Only the page and its assets are public.
    location ^~ /tools/ { return 404; }
    location ~ /\.     { return 404; }
    location = /README.md { return 404; }

    location / {
        try_files $uri $uri/ =404;
    }

    location /assets/ {
        expires 7d;
        add_header Cache-Control "public";
    }

    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Content-Security-Policy "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" always;

    gzip on;
    gzip_types text/html text/css image/svg+xml;
}
```

Notes:
- `add_header` in a `location` block replaces the server-level headers for that location, so `/assets/` responses
  skip the security headers above. That's fine for PNGs; repeat the headers there if you prefer.
- The page has no external requests, so the strict CSP above works as is. If you add a script or stylesheet file
  later, change `'unsafe-inline'` to `'self'` for it.

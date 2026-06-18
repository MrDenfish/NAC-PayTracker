# Deploying NAC-Pay to pch-ledger.com

NAC-Pay runs as its own Docker container on the **CrewRef EC2 box**
(`35.80.137.164`, Ubuntu 22.04), behind the existing **`amis-caddy`** reverse
proxy, with **Cloudflare** in front. It joins the shared `amis-internal` Docker
network and Caddy reaches it as `nac-pay:8000`. CrewRef is untouched except for
appending one site block to its Caddyfile.

```
Browser → Cloudflare (proxied, TLS) → :443 amis-caddy → nac-pay:8000 (uvicorn)
                                                       ↳ app:8501 (crewref, unchanged)
```

## Files in this directory

| File | Purpose |
|------|---------|
| `Dockerfile` | FastAPI + uvicorn image (Python 3.11, unprivileged, healthchecked) |
| `docker-compose.prod.yml` | `nac-pay` service; attaches to external `amis-internal` net |
| `.env.prod.example` | Template for secrets/config — copy to `.env.prod` on the box |
| `Caddyfile.pch-ledger` | Site block to append to `/opt/amis/Caddyfile` |

---

## One-time setup

### 1. Cloudflare — TLS origin certificate (you, in the dashboard)

1. **SSL/TLS → Origin Server → Create Certificate.**
2. Hostnames: `pch-ledger.com`, `*.pch-ledger.com`. Format: PEM. Create.
3. Save the **certificate** body and the **private key**. You'll place them on
   the box as `/opt/amis/certs/pch-ledger.pem` and `…/pch-ledger.key`.
4. **SSL/TLS → Overview:** set the mode to **Full (Strict)**.
5. **SSL/TLS → Edge Certificates:** enable **Always Use HTTPS**.

### 2. Cloudflare — DNS (you, in the dashboard)

Add two **proxied** (orange-cloud) records pointing at the CrewRef box:

| Type | Name | Content | Proxy |
|------|------|---------|-------|
| A | `pch-ledger.com` | `35.80.137.164` | Proxied |
| A | `www` | `35.80.137.164` | Proxied |

The box's firewall already allows 80/443 **only from Cloudflare IP ranges**, so
the proxied record is required — a direct hit won't reach the origin.

### 3. Place the origin cert on the box

```bash
scp -i ~/.ssh/amis-key.pem pch-ledger.pem pch-ledger.key ubuntu@35.80.137.164:/tmp/
ssh -i ~/.ssh/amis-key.pem ubuntu@35.80.137.164
sudo mv /tmp/pch-ledger.pem /tmp/pch-ledger.key /opt/amis/certs/
sudo chmod 600 /opt/amis/certs/pch-ledger.key
```

### 4. Get the code onto the box

```bash
# on the box
sudo mkdir -p /opt/nac-pay && sudo chown ubuntu:ubuntu /opt/nac-pay
git clone https://github.com/MrDenfish/NAC-PayTracker.git /opt/nac-pay
cd /opt/nac-pay
```

### 5. Create `.env.prod` (secrets — never committed)

```bash
cd /opt/nac-pay/deploy
cp .env.prod.example .env.prod
python3 -c "import secrets; print('SESSION_SECRET=' + secrets.token_urlsafe(48))"  # paste into .env.prod
nano .env.prod   # fill SESSION_SECRET; leave STRIPE_BACKEND=fake / EMAIL_BACKEND=console for the first smoke test
```

### 6. Add the Caddy site block + reload

```bash
# append our block to the shared Caddyfile
cat /opt/nac-pay/deploy/Caddyfile.pch-ledger | sudo tee -a /opt/amis/Caddyfile
# validate, then hot-reload Caddy inside its container (no downtime for crewref)
sudo docker exec amis-caddy caddy validate --config /etc/caddy/Caddyfile
sudo docker exec amis-caddy caddy reload   --config /etc/caddy/Caddyfile
```

---

## Deploy / update

```bash
cd /opt/nac-pay/deploy
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
```

To ship new code later: `git pull` then re-run the command above.

## Verify

```bash
# from the box — direct to the container
docker exec nac-pay curl -fsS http://localhost:8000/api/health   # {"status":"ok"}
docker compose -f docker-compose.prod.yml logs -f nac-pay

# from anywhere — through Cloudflare
curl -fsS https://pch-ledger.com/api/health
```

## Rollback

```bash
docker compose -f docker-compose.prod.yml down        # stop nac-pay (crewref unaffected)
# remove the appended block from /opt/amis/Caddyfile, then reload Caddy
```

---

## Notes & caveats

- **Memory:** the box is a 4 GB t3.medium and CrewRef is an ML/RAG app. NAC-Pay
  is light, but check `free -m` after deploy; bump to t3.large if tight.
- **Database:** SQLite on the `nac_pay_data` volume — right for first-real-world
  testing. The DB auto-creates its tables on first request (no migration step).
  Back it up with `docker run --rm -v nac_pay_data:/d -v $PWD:/b alpine cp /d/nac_pay.db /b/`.
- **Stripe / email:** start with `STRIPE_BACKEND=fake` and `EMAIL_BACKEND=console`
  to prove the deploy, then switch to `real`/`resend` once the domain is live.
- **Single worker:** SQLite serializes writes. Move to Postgres + `--workers N`
  when traffic warrants it (set `NAC_PAY_DATABASE_URL` in `.env.prod`).

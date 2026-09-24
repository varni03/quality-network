# Hosting options

Set these environment variables on whichever host you use:
`APP_USERNAME=demo`, `APP_PASSWORD=<a password you'll share>`, `SECRET_KEY=<long random string>`.
Do not set `ANTHROPIC_API_KEY` on a public site. Data resets to the synthetic seed on every
restart, which is what you want for a demo.

## Option V: Vercel (free Hobby plan, no sleep screen)
1. Push the repo to GitHub, then at vercel.com choose **Add New, Project** and import it.
2. Leave the framework on **FastAPI** (Vercel reads `pyproject.toml` and `vercel.json`).
3. Add the environment variables above and click **Deploy**.
Vercel runs the API as serverless functions, so there is no sleeping service and no loading
page. The first request after a quiet spell can take a few seconds while a fresh instance
starts. Each instance keeps its own temporary database in `/tmp`, so anything you create in
the older dashboard tabs (notes, statuses, admin users) can vanish or differ between visits.
The Quality App tabs are unaffected because they keep edits in the browser. Vercel's Hobby
plan is for non-commercial use, which covers a portfolio.

## Option A: Koyeb (free, stays awake)
1. Sign up at koyeb.com and create a **Web Service** from your GitHub repo.
2. Builder: **Dockerfile** (this repo has one). Port: `8000`. Health check path: `/health`.
3. Add the environment variables above and deploy.
Check Koyeb's current free-plan page first: free limits and card requirements change.

## Option B: Render (free) + keep-alive ping
Render's free tier sleeps after 15 minutes idle and shows a loading page while it wakes.
To avoid that, create a free monitor at uptimerobot.com or cron-job.org that requests
`https://YOUR-APP.onrender.com/health` every 10 minutes. This is a workaround, not
something Render officially supports. A single always-on service fits within the
750 free hours per month. The $7/month Starter plan removes sleeping entirely.

## Option C: Google Cloud Run
Needs a billing account (a card on file). Scales to zero with a cold start of a few
seconds instead of a minute. Deploy with `gcloud run deploy --source . --allow-unauthenticated`.
Setting min instances to 1 removes cold starts but is no longer free.

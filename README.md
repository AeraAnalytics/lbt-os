# LBT OS — Lean Business Tracker OS

> The all-in-one operating system for lean businesses. Track leads, revenue, expenses, and AI-powered insights from a single dashboard.

---

## Demo

<!-- 
  VIDEO OPTION 1 — Loom or YouTube (recommended)
  Record a 60-90 second walkthrough, upload to Loom/YouTube, then replace the link below.
  GitHub will render this as a clickable thumbnail if you use the img+link pattern.

  Screenshot to take: your dashboard with data visible, then record yourself navigating
  through Dashboard → Revenue Intelligence → AI Insights → Billing
-->

[![LBT OS Demo](docs/screenshots/demo-thumbnail.png)](https://youtu.be/YOUR_VIDEO_ID)

<!-- 
  VIDEO OPTION 2 — GitHub native video (MP4)
  GitHub now supports embedding MP4s directly in READMEs.
  Just drag-and-drop your .mp4 into the README editor on github.com
  and GitHub will host it and insert the embed tag automatically.
  No external hosting needed for files under 100MB.
-->

---

## Screenshots

### Dashboard
<!-- Screenshot: /app with revenue chart, stat cards, and pipeline visible -->
![Dashboard](docs/screenshots/dashboard.png)

### Revenue Intelligence
<!-- Screenshot: /app/revenue-intelligence showing LTV chart, win/loss rates, stage velocity -->
![Revenue Intelligence](docs/screenshots/revenue-intelligence.png)

### AI Insights (Audit Report)
<!-- Screenshot: /app/insights after running an audit — show the full report expanded -->
![AI Insights](docs/screenshots/ai-insights.png)

### Lead Pipeline
<!-- Screenshot: /app/leads with the kanban or table view populated -->
![Leads](docs/screenshots/leads.png)

### Strategy
<!-- Screenshot: /app/strategy with at least one tab open (SWOT, Competitor, Market Position) -->
![Strategy](docs/screenshots/strategy.png)

### Billing & Plans
<!-- Screenshot: /app/billing showing the three plan cards (Starter / Growth / Scale) -->
![Billing](docs/screenshots/billing.png)

### Connections (Integrations)
<!-- Screenshot: /app/connections showing the available integration tiles -->
![Connections](docs/screenshots/connections.png)

### Messages (Signal Desk)
<!-- Screenshot: /app/messages with the inbox open -->
![Messages](docs/screenshots/messages.png)

---

## Features

| Module | What it does |
|---|---|
| **Dashboard** | Real-time revenue, pipeline, and expense snapshot |
| **Leads** | Full CRM pipeline with stage tracking and conversion metrics |
| **Sales** | Closed deal log with revenue attribution |
| **Customers** | Customer profiles and lifetime value tracking |
| **Expenses** | Expense logging with category breakdown |
| **Revenue Intelligence** | Win/loss by channel, LTV analysis, stage velocity |
| **AI Insights** | GPT-4o powered monthly business audit with recommendations |
| **Strategy** | SWOT analysis, competitor tracking, market positioning |
| **Signal Desk** | Unified inbox for customer messages |
| **Connections** | Integration hub (QuickBooks, Stripe, HubSpot, and more) |
| **Billing** | Stripe-powered subscription management |

### Plan Tiers

| Plan | Price | AI Audits / mo |
|---|---|---|
| Starter | Free | 3 |
| Growth | $129/mo | 20 |
| Scale | $299/mo | Unlimited |
| Enterprise | Custom | Unlimited |

---

## Tech Stack

**Frontend** — React 18, Vite, Tailwind CSS, Clerk Auth, React Router v6

**Backend** — FastAPI (Python), Supabase (PostgreSQL), Stripe, OpenAI GPT-4o

**Infrastructure** — Railway (backend), Vercel (frontend), Supabase (database + storage)

---

## Local Development

### Prerequisites
- Node 18+
- Python 3.11+
- A Supabase project
- A Stripe account (test mode)
- A Clerk application
- An OpenAI API key

### 1. Clone

```bash
git clone https://github.com/AeraAnalytics/lbt-os.git
cd lbt-os
```

### 2. Backend

```bash
cd backend
cp .env.example .env
# Fill in .env with your keys (see section below)

pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### 3. Frontend

```bash
cd frontend
cp .env.example .env.local
# Add VITE_CLERK_PUBLISHABLE_KEY

npm install
npm run dev
```

App runs at `http://localhost:5173`. All `/api/*` requests proxy to the backend.

### Environment Variables

**Backend `.env`**
```
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
OPENAI_API_KEY=
CLERK_SECRET_KEY=
```

**Frontend `.env.local`**
```
VITE_CLERK_PUBLISHABLE_KEY=
```

### Seed demo data

```bash
cd backend
python scripts/seed_dummy_pipeline.py
```

---

## Database Migrations

Run all migrations in order from the `supabase/` folder in the Supabase SQL editor:

```
migration_add_integrations.sql
migration_messages.sql
migration_revenue_intelligence.sql
migration_audit_report_fields.sql
migration_csv_import_logs.sql
migration_data_portability.sql
migration_visitor_events.sql
migration_stripe_events.sql
migration_signal_desk_analytics.sql
migration_signal_desk_notifications.sql
migration_message_storage_bucket.sql
```

---

## Deploy

See [docs/deployment.md](docs/deployment.md) for Railway + Vercel step-by-step.

---

## Adding Features

The codebase follows a consistent pattern — adding a new page takes 4 steps:

**1. Create the backend router** (`backend/app/routers/your_feature.py`)
```python
from fastapi import APIRouter, Depends
from app.auth import get_current_org

router = APIRouter(prefix="/your-feature", tags=["your-feature"])

@router.get("/")
async def get_data(auth=Depends(get_current_org)):
    return {"data": []}
```

**2. Register the router** in `backend/app/main.py`
```python
from app.routers import your_feature
app.include_router(your_feature.router, prefix="/api/v1")
```

**3. Create the frontend page** (`frontend/src/pages/YourFeature.jsx`)
```jsx
import { useQuery } from '@tanstack/react-query'
import api from '../lib/api'

export default function YourFeature() {
  const { data } = useQuery({
    queryKey: ['your-feature'],
    queryFn: () => api.get('/your-feature/').then(r => r.data)
  })
  return <div>{/* your UI */}</div>
}
```

**4. Wire up the route + sidebar**
- Add to `frontend/src/App.jsx` under the `/app` route
- Add a nav item in `frontend/src/components/layout/Sidebar.jsx`

---

## How to Add Screenshots

1. Create the folder: `docs/screenshots/`
2. Take screenshots at **1440×900** or wider for best quality
3. Name them to match the filenames in this README (e.g. `dashboard.png`)
4. Drop them in `docs/screenshots/` and push — GitHub will render them automatically

**Recommended tool:** Use macOS `Cmd+Shift+4` for screenshots, or [CleanShot X](https://cleanshot.com) for annotated shots.

## How to Add Videos

**Option A — MP4 (simplest, hosted by GitHub):**
1. Open this README on github.com → click the pencil (edit) icon
2. Drag and drop your `.mp4` file anywhere into the editor
3. GitHub uploads it and inserts the embed tag — commit the change

**Option B — Loom/YouTube thumbnail link:**
1. Upload your video to Loom or YouTube
2. Take a screenshot of the video as your thumbnail, save to `docs/screenshots/demo-thumbnail.png`
3. Replace `YOUR_VIDEO_ID` in the Demo section above with your video ID

---

## License

MIT

# SocialProof — Deployment Structure

```
/
├── frontend/       → Deploy to Vercel
│   ├── pages/      → HTML pages (static)
│   ├── vercel.json → Vercel routing config
│   ├── build.sh    → Injects BACKEND_URL at build time
│   └── env-config.js → Runtime backend URL (auto-generated)
│
└── backend/        → Deploy to Railway
    ├── main.py     → FastAPI entry point (API-only)
    ├── config.py   → Env var config
    ├── railway.json → Railway deploy config
    ├── nixpacks.toml → Build config (tesseract, spaCy)
    ├── Procfile    → Start command
    └── requirements.txt
```

See DEPLOYMENT_GUIDE.md for step-by-step instructions.

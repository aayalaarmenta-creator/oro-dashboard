# Dashboard del oro (XAU/USD)

Página que se regenera sola cada ~15 minutos con GitHub Actions y se publica en GitHub Pages:
gráfica 4H/diaria, niveles, escenarios condicionales del día, eventos y noticias. El precio spot
se actualiza en la página cada 30 segundos.

Solo análisis, no es una recomendación de inversión.

- `gold.py`: copia del script del skill `trader-gold` (fuente: `~/.claude/skills/trader-gold/scripts/gold.py`).
- `.github/workflows/dashboard.yml`: el horario y la publicación.
- Para forzar una actualización: pestaña **Actions** → "Dashboard del oro" → **Run workflow**.

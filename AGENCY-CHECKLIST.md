# Agency report checklist vs our dashboard

What top agencies put in weekly client reports ([Prooflytics](https://prooflytics.io/blog/weekly-marketing-report-template), [AgencyAnalytics](https://agencyanalytics.com/blog/weekly-marketing-reports), [5day.io](https://5day.io/blog/create-weekly-marketing-report/)) and how we cover it.

| Agency standard | Our dashboard section | Status |
|-----------------|----------------------|--------|
| Executive summary (4–6 lines) | Executive summary | ✅ Added |
| KPI vs target (this / last / goal) | KPI vs target table | ✅ Added |
| Channel performance + WoW | Channel performance with WoW % | ✅ Added |
| Budget pacing (MTD vs plan) | Budget pacing | ✅ Added (set plans in `config.json`) |
| Week-over-week trends (8–12 wks) | Weekly trend + daily orders | ⚠️ 2 weeks now — archive `history.json` weekly |
| Anomaly explanation (cause + response) | Anomalies table | ✅ Added |
| Recommended actions with owners | Spend / Pull / Create / Watch + Next week priorities | ✅ Added |
| Campaign status update | Campaign status | ✅ Added |
| Creative: top, fatiguing, new tests | Creative performance | ✅ Added |
| Experiment / A-B results | New tests under Creative | ⚠️ Manual until test registry |
| Agency activity log | Agency activity log | ✅ Added (Blend Meta log) |
| Challenges and roadblocks | Challenges table | ✅ Added |
| Organic / email / SEO snapshot | Organic and email (GA4) | ✅ Added |
| CPA / MER / AOV | KPI row | ✅ Added |
| Data appendix | Meta ads, GA4 funnel, data confidence | ✅ Had + expanded |

## Still to automate (next builds)

1. **8–12 week history** — append to `data/history.json` each Monday
2. **Budget MTD from Blend** — replace estimates with live MTD pull
3. **Shopify script SSL** — use curl backend on macOS Python
4. **Email send** — Cursor Automation + Gmail/Slack
5. **Persistent URL** — GitHub Pages on `dashboard/`
6. **Experiment registry** — JSON file when running A/B tests

## Truth hierarchy (never change)

1. Shopify paid orders and UTM $
2. Blend spend
3. GA4 funnel direction
4. Platform purchase counts (reference + overclaim flag)

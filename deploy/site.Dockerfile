FROM node:24-bookworm-slim
WORKDIR /srv/site
COPY site/index.html site/styles.css site/icons.js site/api.js site/billing.js site/app.js site/serve.js site/manifest.webmanifest ./
COPY site/assets/dry-logo.png assets/
COPY site/assets/fonts/ assets/fonts/
USER node
EXPOSE 5173
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["node", "-e", "fetch('http://127.0.0.1:5173/').then(r=>{if(!r.ok) process.exit(1)}).catch(()=>process.exit(1))"]
CMD ["node", "serve.js"]

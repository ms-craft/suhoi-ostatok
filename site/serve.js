/* Local static server and same-origin transport for the existing FastAPI app. */
const http = require('node:http');
const https = require('node:https');
const fs = require('node:fs/promises');
const path = require('node:path');

const ROOT = __dirname;
const HOST = process.env.SITE_HOST || '127.0.0.1';
const PORT = Number(process.env.SITE_PORT || process.env.PORT || 5173);
const API = new URL(process.env.API_ORIGIN || 'http://127.0.0.1:8000');
if (!['http:', 'https:'].includes(API.protocol) || API.username || API.password ||
    API.pathname !== '/' || API.search || API.hash) {
  throw new Error('API_ORIGIN must be an HTTP(S) origin, for example http://127.0.0.1:8000');
}

const MIME = new Map([
  ['.html', 'text/html; charset=utf-8'],
  ['.css', 'text/css; charset=utf-8'],
  ['.js', 'text/javascript; charset=utf-8'],
  ['.webmanifest', 'application/manifest+json; charset=utf-8'],
  ['.png', 'image/png'],
  ['.woff2', 'font/woff2'],
]);
const SITE_FILES = new Set([
  '/index.html', '/styles.css', '/icons.js', '/api.js', '/billing.js', '/app.js',
  '/manifest.webmanifest', '/assets/dry-logo.png', '/assets/fonts/fonts.css',
  '/assets/katex/katex.min.css', '/assets/katex/katex.min.js',
]);
const HOP_HEADERS = new Set([
  'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
  'te', 'trailer', 'transfer-encoding', 'upgrade',
]);

function forwardHeaders(headers, request = false) {
  const connected = new Set((headers.connection || '').split(',').map(value => value.trim().toLowerCase()));
  return Object.fromEntries(Object.entries(headers).filter(([name]) =>
    !HOP_HEADERS.has(name) && !connected.has(name) &&
    (!request || (name !== 'host' && name !== 'forwarded' && !name.startsWith('x-forwarded-'))),
  ));
}

function proxy(req, res, url) {
  const client = API.protocol === 'https:' ? https : http;
  const upstream = client.request(API, {
    method: req.method,
    path: url.pathname + url.search,
    headers: forwardHeaders(req.headers, true),
  }, response => {
    res.writeHead(response.statusCode, forwardHeaders(response.headers));
    response.on('error', () => res.destroy());
    response.pipe(res);
  });
  upstream.setTimeout(url.pathname === '/v1/question-import' ? 150_000 : 60_000, () => upstream.destroy());
  upstream.on('error', () => {
    if (res.destroyed || res.writableEnded) return;
    if (res.headersSent) {
      res.destroy();
      return;
    }
    res.writeHead(502, {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store'});
    res.end(JSON.stringify({error: {
      code: 'api_unavailable', message: 'Сервер проверки недоступен. Повторите попытку.',
    }}));
  });
  res.on('close', () => {
    if (!res.writableEnded) upstream.destroy();
  });
  req.on('error', () => upstream.destroy());
  req.pipe(upstream);
}

function notFound(res) {
  res.writeHead(404, {'Content-Type': 'text/plain; charset=utf-8'});
  res.end('Не найдено');
}

http.createServer(async (req, res) => {
  if (!req.url?.startsWith('/') || req.url.startsWith('//')) {
    notFound(res);
    return;
  }
  let url;
  try {
    url = new URL(req.url, 'http://localhost');
  } catch {
    notFound(res);
    return;
  }
  if (url.origin !== 'http://localhost') {
    notFound(res);
    return;
  }
  if (url.pathname === '/v1' || url.pathname.startsWith('/v1/')) {
    proxy(req, res, url);
    return;
  }
  if (!['GET', 'HEAD'].includes(req.method)) {
    res.writeHead(405, {Allow: 'GET, HEAD'});
    res.end();
    return;
  }
  const name = url.pathname === '/' ? '/index.html' : url.pathname;
  if (!SITE_FILES.has(name) && !/^\/assets\/fonts\/font-\d{2}\.woff2$/.test(name) &&
      !/^\/assets\/katex\/fonts\/KaTeX_[A-Za-z0-9_-]+\.woff2$/.test(name)) {
    notFound(res);
    return;
  }
  let contents;
  try {
    contents = await fs.readFile(path.join(ROOT, name));
  } catch {
    notFound(res);
    return;
  }
  res.writeHead(200, {
    'Content-Type': MIME.get(path.extname(name)),
    'Content-Length': contents.length,
    'Cache-Control': 'no-cache',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'Content-Security-Policy': "default-src 'self'; img-src 'self' blob:; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
  });
  res.end(req.method === 'HEAD' ? undefined : contents);
}).listen(PORT, HOST, () => {
  console.log('Сухой остаток · http://' + HOST + ':' + PORT);
});

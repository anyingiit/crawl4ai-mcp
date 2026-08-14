# Rayobyte Web Scraper API contract

Captured 2026-08-13 by probing the live endpoint with the account API key
(the docs site requires dashboard sign-in; the public product page publishes
no request contract). Probe targets were harmless static pages and a
deliberately missing path.

## Request

```bash
curl 'https://api.scraping.rayobyte.com/?token=<API-KEY>&url=https://example.com/'
```

- HTTP method: `GET`
- Endpoint: `https://api.scraping.rayobyte.com/`
- API-key location: `token` query parameter
- Target URL field: `url` query parameter
- Optional custom headers: `customHeaders=true` (header values passed via
  `customHeaders` body/form; not exercised here)
- JavaScript rendering: **enabled by default**. Verified by fetching
  `https://web-scraping.dev/js-links`: the JS-injected anchor
  `/js-links-target` was present in `result` without any extra parameter.
  `render=true` is **not** a supported parameter (probe returned a 404).
- No request body for the basic scrape.

## Success response

Content type `application/json`, HTTP 200 even when the target itself failed
(target status is inside the body):

```json
{
  "status": "SUCCESS",
  "date": "Thu, 13 Aug 2026 10:02:17 GMT",
  "httpCode": 200,
  "headers": { "...": "request headers used by the scraper" },
  "taskId": "768ce64a-a952-4e24-af2d-d81e78f55725",
  "result": "<full target HTML>"
}
```

- Target HTTP status: `httpCode`
- Page HTML: `result`

A 404 target is reported as `"status":"SUCCESS"` with `"httpCode":404` and
the 404 page HTML in `result`; the API counts only successful scrapes, so the
provider must surface `httpCode` as `FetchResult.status_code` and let the
cascade classify.

## Error responses

Authentication failure returns HTTP 200 with:

```json
{
  "status": "FAIL",
  "date": "Thu, 13 Aug 2026 10:02:26 GMT",
  "statusCode": 401,
  "error": "Invalid or blocked token: bogus-key-123",
  "error_details": "Invalid or blocked token: bogus-key-123"
}
```

Credit exhaustion, rate limiting, and invalid targets follow the same
`FAIL` envelope with `statusCode` (e.g. 402/429) and `error`. The provider
normalizes any `FAIL` response into a failed `FetchResult` carrying the
status code and message; it never raises into the cascade.

## Provider notes

- `CostKind.RAYOBYTE_CREDIT`
- `availability().ready` only when both `RAYOBYTE_API_URL` and
  `RAYOBYTE_API_KEY` are configured.

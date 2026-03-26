# Old Snapshot Preview Method

Reference for reverting the snapshot card preview back to the original approach.

## What the old method did

Each snapshot card immediately loaded its iframe using the `id_` modifier and set `src` directly (no lazy loading):

```html
<iframe src="https://web.archive.org/web/{timestamp}id_/{original_url}"
        loading="lazy"
        sandbox="allow-scripts allow-same-origin"></iframe>
```

And the page size was 12 cards:

```js
const params = new URLSearchParams({ page: currentPage, limit: 12 });
```

## What the new method does differently

| | Old | New |
|---|---|---|
| iframe attr | `src="..."` (loads immediately) | `data-src="..."` (loads on scroll) |
| Loading trigger | All at page render | IntersectionObserver (80px before visible) |
| Cards per page | 12 | 6 |

## How to revert to old method

In `app/static/index.html`, make these two changes:

**1. In `renderSnapshots`, change `data-src` back to `src`:**

```js
// REVERT THIS:
<iframe data-src="${escAttr(previewUrl)}" loading="lazy" sandbox="allow-scripts allow-same-origin"></iframe>

// BACK TO:
<iframe src="${escAttr(previewUrl)}" loading="lazy" sandbox="allow-scripts allow-same-origin"></iframe>
```

**2. Change limit back to 12:**

```js
// REVERT THIS:
const params = new URLSearchParams({ page: currentPage, limit: 6 });

// BACK TO:
const params = new URLSearchParams({ page: currentPage, limit: 12 });
```

**3. Remove the IntersectionObserver block at the end of `renderSnapshots`:**

```js
// REMOVE THIS ENTIRE BLOCK:
const observer = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      const iframe = entry.target;
      if (iframe.dataset.src) {
        iframe.src = iframe.dataset.src;
        iframe.removeAttribute('data-src');
      }
      observer.unobserve(iframe);
    }
  });
}, { rootMargin: '80px' });
document.querySelectorAll('#snapshotGrid iframe[data-src]').forEach(f => observer.observe(f));
```

## Why the old method was slow

The `id_` modifier on Wayback URLs (`/web/{timestamp}id_/{url}`) serves the **raw original archived HTML** without any Wayback Machine processing. This means:

- All CSS/JS/image references still point to the original server (e.g. `https://example.com/style.css`)
- Those servers are often dead or slow — the browser makes dozens of failed requests per iframe
- With 12 iframes loading simultaneously, all these dead requests compete for bandwidth
- Result: 10-30 second delays before pages visually render

The new lazy method loads iframes one at a time as the user scrolls, so each gets full bandwidth.

## Why `id_` was kept (not switched to regular Wayback URL)

The user explicitly requested keeping `id_`. The alternative (dropping `id_`) would use:
```
https://web.archive.org/web/{timestamp}/{url}
```
This routes all resources through Wayback's proxy (faster) but adds the Wayback toolbar banner and rewrites links. User preferred the raw view.

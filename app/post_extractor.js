// Read only the currently rendered page. This script makes no network requests.
const messageSelector = '[data-ad-preview="message"], [data-ad-rendering-role="story_message"], [data-ad-comet-preview="message"]';
const expectedUrl = (() => {
  try { return new URL(arguments[0]); } catch { return null; }
})();
const facebookHosts = new Set(['facebook.com', 'www.facebook.com', 'm.facebook.com']);
const expectedMatch = expectedUrl?.pathname.match(/^\/groups\/([A-Za-z0-9._-]+)\/?$/);
if (!expectedUrl || expectedUrl.protocol !== 'https:' || !facebookHosts.has(expectedUrl.hostname) || expectedUrl.username || expectedUrl.password || (expectedUrl.port && expectedUrl.port !== '443') || !expectedMatch) return [];
const expectedGroup = expectedMatch[1].toLowerCase();

function visible(element) {
  if (!element || !element.isConnected) return false;
  for (let node = element; node && node.nodeType === 1; node = node.parentElement) {
    if (node.hidden || node.getAttribute('aria-hidden') === 'true') return false;
    const style = getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse' || style.opacity === '0') return false;
  }
  return true;
}

function renderedText(element) {
  return element && typeof element.innerText === 'string' ? element.innerText.trim() : '';
}

function permalink(anchor) {
  try {
    const url = new URL(anchor.href);
    if (url.protocol !== 'https:' || !facebookHosts.has(url.hostname) || url.username || url.password || (url.port && url.port !== '443')) return null;
    if ([...url.searchParams.keys()].some(key => ['comment_id', 'reply_comment_id'].includes(key.toLowerCase()))) return null;
    const match = url.pathname.match(/^\/groups\/([A-Za-z0-9._-]+)\/(posts|permalink)\/([A-Za-z0-9._-]+)\/?$/);
    if (!match || match[1].toLowerCase() !== expectedGroup) return null;
    return {identity:match[3], url:'https://www.facebook.com/groups/' + match[1] + '/' + match[2] + '/' + match[3] + '/'};
  } catch { return null; }
}

const markers = [...document.querySelectorAll(messageSelector)];
// Multiple message attributes often wrap the same rendered story body. A nested
// message is never a separate top-level story when an outer body already owns it.
const rawBodies = markers.filter(element => !markers.some(other => other !== element && other.contains(element)));
const articleOwner = element => element.closest('[role="article"]');

function nestedStory(body, bodies) {
  const owner = articleOwner(body);
  if (!owner) return false;
  for (let parent = owner.parentElement; parent; parent = parent.parentElement) {
    if (parent.getAttribute('role') === 'feed') break;
    if (parent.getAttribute('role') !== 'article') continue;
    // A pure article wrapper owns no body directly and is not itself a post.
    if (bodies.some(other => other !== body && articleOwner(other) === parent)) return true;
  }
  return false;
}

// Invisible or empty neighboring markers still establish a structural boundary.
// Their surrounding header links must not be borrowed by a visible story.
const rawStories = rawBodies.filter(body => !nestedStory(body, rawBodies));
const stories = rawStories.filter(body => visible(body) && renderedText(body));
const precedes = (element, body) => !!(element.compareDocumentPosition(body) & Node.DOCUMENT_POSITION_FOLLOWING);

function headerElement(element, body) {
  if (!visible(element) || element.closest(messageSelector) || element.contains(body)) return false;
  const owner = element.closest('[role="article"]');
  if (owner && !owner.contains(body)) return false;
  if (element.closest('[role="comment"], [data-ad-rendering-role="comment"]')) return false;
  return true;
}

function nearestHeading(boundary, body) {
  let headings = [...boundary.querySelectorAll('h2, h3, h4, [role="heading"]')];
  if (!headings.length) headings = [...boundary.querySelectorAll('strong')];
  const preceding = headings.filter(heading => headerElement(heading, body) && precedes(heading, body) && renderedText(heading));
  const heading = preceding[preceding.length - 1];
  if (!heading) return null;
  // A preceding image-only story can have no message marker. Its media between
  // its heading and this body establishes a separate story, not a shared header.
  const interveningMedia = [...boundary.querySelectorAll('img, video, picture, [role="img"]')]
    .some(media => !heading.contains(media) && visible(media) && precedes(heading, media) && precedes(media, body));
  return interveningMedia ? null : heading;
}

function headerLinks(boundary, body, heading) {
  if (!heading) return [];
  const links = [...boundary.querySelectorAll('a[href]')]
    .filter(anchor => headerElement(anchor, body))
    .filter(anchor => precedes(anchor, body) && (heading.contains(anchor) || precedes(heading, anchor)))
    .map(anchor => ({anchor, post:permalink(anchor)}))
    .filter(item => item.post);
  // Later headings can be comments or shared stories. This pilot keeps them
  // unlinked instead of guessing from their visual position.
  return links;
}

function authorFrom(boundary, body) {
  const heading = nearestHeading(boundary, body);
  if (!heading) return '';
  const authors = [...heading.querySelectorAll('a')]
    .filter(anchor => headerElement(anchor, body) && renderedText(anchor));
  const author = authors[authors.length - 1];
  return renderedText(author);
}

const results = [];
for (const body of stories) {
  let safeBoundary = body;
  let found = null;
  const feed = body.closest('[role="feed"]');
  let feedChild = body;
  if (feed) {
    while (feedChild.parentElement && feedChild.parentElement !== feed) feedChild = feedChild.parentElement;
  }
  for (let boundary = body.parentElement, depth = 0; boundary && depth < 20; boundary = boundary.parentElement, depth++) {
    if (boundary === document.body || boundary === document.documentElement || boundary.getAttribute('role') === 'feed') break;
    if (feed && boundary !== feedChild && !feedChild.contains(boundary)) break;
    if (rawStories.some(other => other !== body && boundary.contains(other))) break;
    safeBoundary = boundary;
    const heading = nearestHeading(boundary, body);
    const links = headerLinks(boundary, body, heading);
    const identities = new Set(links.map(item => item.post.identity));
    if (identities.size > 1) break;
    if (identities.size === 1) {
      found = {boundary, link:links[0]};
      break;
    }
    // Stop at the first local header even if its own timestamp has no post ID.
    // Looking higher could borrow an earlier image-only story's permalink.
    if (heading) break;
  }
  const boundary = found?.boundary || safeBoundary;
  const anchor = found?.link.anchor;
  const timestampElement = anchor?.querySelector('time[datetime], abbr[data-utime], [data-utime]');
  results.push({
    author:boundary ? authorFrom(boundary, body) : '',
    content:renderedText(body),
    posted_at:anchor ? (anchor.getAttribute('aria-label') || anchor.getAttribute('title') || renderedText(anchor)) : '',
    published_at:timestampElement?.getAttribute('datetime') || timestampElement?.getAttribute('data-utime') || anchor?.getAttribute('data-utime') || '',
    url:found?.link.post.url || ''
  });
}
return results;

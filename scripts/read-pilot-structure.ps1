# Inspect only the pilot's existing, confirmed WebDriver page.
# No navigation, refresh, scrolling, clicks, cookies, or post text are returned.
param([switch]$ValidateExtractor)

$ErrorActionPreference = 'Stop'
$state = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/api/state' -TimeoutSec 5
if (-not $state.status.browser_open -or -not $state.status.resolved_group_url) {
    throw 'The pilot has no confirmed browser session to inspect.'
}

$processes = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'msedgedriver.exe' OR Name = 'chromedriver.exe'")
$pilotIds = @($processes | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match '(^|[\s\\/])verify\.py([\s"]|$)' } | ForEach-Object { $_.ProcessId })
$drivers = @($processes | Where-Object { $_.Name -match '^(msedgedriver|chromedriver)\.exe$' -and $_.ParentProcessId -in $pilotIds })
if ($drivers.Count -ne 1) {
    throw 'Expected exactly one WebDriver owned by the verification process.'
}
$portMatch = [regex]::Match($drivers[0].CommandLine, '--port=(\d+)')
if (-not $portMatch.Success) {
    throw 'The driver did not expose a loopback service port.'
}
$serviceUrl = 'http://127.0.0.1:' + $portMatch.Groups[1].Value
$sessions = @((Invoke-RestMethod -Uri ($serviceUrl + '/sessions') -TimeoutSec 5).value)
if ($sessions.Count -ne 1) {
    throw 'Expected exactly one existing verification browser session.'
}
$sessionId = $sessions[0].id
if (-not $sessionId) { $sessionId = $sessions[0].sessionId }
if (-not $sessionId) { throw 'No session identifier returned.' }

# Validate both hosts and the exact group path before inspecting any page data.
# Host variants, a trailing slash, and sorting/query parameters are normalized.
$guard = @'
const pilotFacebookHosts = new Set(['facebook.com', 'www.facebook.com', 'm.facebook.com']);
function normalizePilotGroup(value) {
  try {
    const url = new URL(value);
    const group = url.pathname.match(/^\/groups\/([A-Za-z0-9._-]+)\/?$/);
    if (url.protocol !== 'https:' || !pilotFacebookHosts.has(url.hostname) || url.username || url.password || (url.port && url.port !== '443') || !group) return null;
    return 'https://www.facebook.com/groups/' + group[1].toLowerCase();
  } catch { return null; }
}
const expectedPilotGroup = normalizePilotGroup(arguments[0]);
if (!expectedPilotGroup || normalizePilotGroup(location.href) !== expectedPilotGroup) {
  return {error:'The current page differs from the confirmed Facebook group.'};
}
'@

$lf = [string][char]10
if ($ValidateExtractor) {
    $projectPath = Split-Path -Parent $PSScriptRoot
    $metadataPath = Join-Path $projectPath 'app/post_metadata.js'
    $extractorPath = Join-Path $projectPath 'app/post_extractor.js'
    $metadata = Get-Content -LiteralPath $metadataPath -Raw -Encoding UTF8
    $rendered = Get-Content -LiteralPath $extractorPath -Raw -Encoding UTF8

    # Keep this composition identical to app.pilot_browser.post_extractor_script.
    $composed = $metadata + $lf + 'const renderedPosts=(function(){' + $lf + $rendered + $lf + '}).apply(null,arguments);' + $lf + 'return renderedPosts.concat(extractLoadedPostMetadata(arguments[0]));'
    $summary = @'
return {
  posts: posts.length,
  linked: posts.filter(post => !!post.url).length,
  metadataRecords: metadataScan.records.length,
  metadataLimitExceeded: metadataScan.limit_exceeded,
  metadataLimitReason: metadataScan.limit_reason || '',
  metadataDuplicateChecks: metadataScan.records.flatMap((record, index) => metadataScan.records.slice(index + 1).filter(other => record.post_id === other.post_id).map(other => ({
    sameText: record.text === other.text,
    sameTrimmedText: record.text.trim() === other.text.trim(),
    sameActors: JSON.stringify(record.actors) === JSON.stringify(other.actors),
    sameUrl: record.url === other.url,
    urlRoutes: [record.url, other.url].map(value => new URL(value).pathname.split('/')[3])
  }))).slice(0, 8),
  samples: posts.slice(0, 8).map(post => ({
    contentLength: String(post.content || '').length,
    hasAuthor: !!post.author,
    hasLink: !!post.url,
    hasTime: !!post.posted_at,
    hasPrecisePublicationTime: !!post.published_at
  }))
};
'@
    $script = $guard + $lf + $metadata + $lf + 'const metadataScan=scanPostMetadata(arguments[0]);' + $lf + 'const posts=(function(){' + $lf + $composed + $lf + '}).apply(null,arguments);' + $lf + $summary
} else {
    $structure = @'
const messageSelector = '[data-ad-preview="message"],[data-ad-rendering-role="story_message"],[data-ad-comet-preview="message"]';
const messages = [...document.querySelectorAll(messageSelector)];
function visiblePilotMessage(element) {
  if (!element.isConnected || element.getClientRects().length === 0) return false;
  for (let node = element; node && node.nodeType === 1; node = node.parentElement) {
    if (node.hidden || node.getAttribute('aria-hidden') === 'true') return false;
    const style = getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse' || style.opacity === '0') return false;
  }
  return true;
}
const bodyLengths = messages.map(message => (message.innerText || '').trim().length);
const visibleMessages = messages.filter(visiblePilotMessage);
const feedAncestors = [];
for (let node = document.querySelector('[role="feed"]'); node && feedAncestors.length < 12; node = node.parentElement) {
  feedAncestors.push({tag:node.tagName,role:node.getAttribute('role'),overflow:getComputedStyle(node).overflowY,
    height:node.clientHeight,scrollHeight:node.scrollHeight,top:node.scrollTop});
}
function lengthStats(lengths) {
  return {
    count: lengths.length,
    min: lengths.length ? Math.min(...lengths) : 0,
    max: lengths.length ? Math.max(...lengths) : 0,
    total: lengths.reduce((sum, length) => sum + length, 0)
  };
}
return {
  articles: document.querySelectorAll('[role="article"]').length,
  visibility: document.visibilityState,
  focused: document.hasFocus(),
  loadingIndicators: document.querySelectorAll('[role="progressbar"],[aria-busy="true"]').length,
  sortParameter: new URL(location.href).searchParams.get('sorting_setting'),
  feedControls: [...document.querySelectorAll('button,[role="button"],[role="status"],[role="alert"]')]
    .filter(visiblePilotMessage)
    .map(node => (node.innerText || node.getAttribute('aria-label') || '').trim())
    .filter(text => text.length < 80 && /^(?:Show more|See more|Load more|New posts|Retry|\u986f\u793a\u66f4\u591a|\u67e5\u770b\u66f4\u591a|\u67e5\u770b\u8f03\u820a|\u66f4\u591a\u8cbc\u6587|\u65b0\u8cbc\u6587|\u6700\u76f8\u95dc|\u91cd\u8a66|\u91cd\u65b0\u8f09\u5165)/i.test(text)).slice(0, 12),
  feeds: document.querySelectorAll('[role="feed"]').length,
  feedUnits: document.querySelectorAll('[data-pagelet^="FeedUnit_"]').length,
  messages: messages.length,
  visible: visibleMessages.length,
  nonempty: bodyLengths.filter(length => length > 0).length,
  visibleNonempty: visibleMessages.filter(message => (message.innerText || '').trim().length > 0).length,
  feedAncestors,
  documentScroll: {height:document.scrollingElement?.clientHeight,scrollHeight:document.scrollingElement?.scrollHeight,top:document.scrollingElement?.scrollTop},
  visibleDialogs: [...document.querySelectorAll('[role="dialog"]')].filter(visiblePilotMessage).length,
  bodyLengths: lengthStats(bodyLengths),
  visibleBodyLengths: lengthStats(visibleMessages.map(message => (message.innerText || '').trim().length))
};
'@
    $script = $guard + $lf + $structure
}

$payload = @{script=$script; args=@($state.status.resolved_group_url)} | ConvertTo-Json -Depth 5 -Compress
$sessionPath = '/session/' + [uri]::EscapeDataString($sessionId) + '/execute/sync'
$result = Invoke-RestMethod -Uri ($serviceUrl + $sessionPath) -Method Post -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($payload)) -TimeoutSec 15
$result.value | ConvertTo-Json -Depth 6

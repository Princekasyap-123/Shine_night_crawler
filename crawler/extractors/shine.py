import logging
import re
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# --- Confirmed against real Shine search-results HTML ---------------------

# Sidebar count uses a different id ("#id_candidates_count") on some page
# variants but the same number; primary selector first, fallback second.
_RESULT_COUNT_SELECTORS = (
    "strong.candidate_paginator_count",
    "#id_candidates_count",
)

# Appears twice on the page (pagination-top and pagination-bottom), both
# with identical "Page: X of Y" text — query_selector taking the first
# match is harmless here.
_PAGINATION_BAR_SELECTOR = "div.pagination.srchp"
_PAGE_TEXT_PATTERN = re.compile(r"Page:\s*(\d+)\s*of\s*(\d+)")

# The pagination bar holds two role="button" anchors (< and >). The next
# arrow is greyed out via the "cls_noclick" class when on the last page;
# it's the last of the two anchors.
_PAGINATION_ARROW_SELECTOR = "a[role='button']"
_DISABLED_ARROW_CLASS = "cls_noclick"

# Each candidate card on the advanced-search results list has an id of
# the form "cnd_div_<hash>" (confirmed against the real recruiter.shine.com
# results DOM via the WhiteForce extension's own getShineCandidateCards()).
# The attribute-prefix selector alone would also match nested elements
# that happen to share the "cnd_div_" prefix, so results are filtered
# through the same id-shape check the extension itself uses.
CANDIDATE_CARD_SELECTOR = "[id^='cnd_div_']"
_CANDIDATE_CARD_ID_PATTERN = re.compile(r"^cnd_div_[a-zA-Z0-9]+$")

# Confirmed on the same card: the phone number is already unmasked in the
# DOM as a data attribute — no reveal click needed, unlike most other
# portals (per the "never click reveal buttons" rule for this project).
# Used to populate CandidateRecord.phone at extraction time, and sent
# as the "phone" field on submission.
_CARD_PHONE_SELECTOR = "a.srp_action_btn--viewphone[data-full-mobile], a.srp_action_btn--viewphone[data-masked]"

# Same masking heuristic content.js's looksMasked() uses: 4+ consecutive
# mask characters, or a mix of digits and mask characters in the same
# string — either means the number wasn't actually revealed.
_MASK_RUN_PATTERN = re.compile(r"[x*•●]{4,}", re.IGNORECASE)
_HAS_DIGIT_PATTERN = re.compile(r"\d")
_HAS_MASK_CHAR_PATTERN = re.compile(r"[x*•●]", re.IGNORECASE)


def _looks_masked(value: str) -> bool:
    if not value:
        return True
    if _MASK_RUN_PATTERN.search(value):
        return True
    return bool(_HAS_DIGIT_PATTERN.search(value) and _HAS_MASK_CHAR_PATTERN.search(value))


# Same fixed junk-number list content.js's isPhoneIgnored() rejects —
# "8010062222" is Shine's own support/helpdesk number (not a candidate's),
# and "2147483647" is an int32-overflow placeholder value. Neither is a
# real candidate phone number even though both pass the masking check.
_NUMBERS_TO_IGNORE = ("8010062222", "2147483647")


def _is_phone_ignored(cleaned: str) -> bool:
    # Mirrors isPhoneIgnored()'s substring containment check exactly
    # (cleanPhoneToTest.includes(cleanIgnoreNumber)), not an equality
    # check.
    return any(ignored in cleaned for ignored in _NUMBERS_TO_IGNORE)

# --- Search submission ------------------------------------------------
# CONFIRMED (live test): Shine's advanced search does NOT work via a
# constructible URL. Submitting a search — even a brand-new one, not
# just a saved/recent one — redirects to a server-generated
# "?suid=<hash>" results URL (e.g.
# recruiter.shine.com/recruiter/search/advanced/?suid=3aa868...). There
# is no way to build that URL in advance; the form has to actually be
# submitted each time and the resulting suid followed.
#
# Confirmed selectors, from the real advanced-search form HTML
# (id="id_form_advanced", method="post", action="/recruiter/search/advanced/"):
#   - "Any of these keywords" input: #id_any_keyword. Typing text and
#     pressing Enter converts it into a tag chip (visible in the DOM as
#     .cls_tagger .tag[data-tag-value=...]) and populates a hidden
#     name="any" field with the committed value — but does NOT submit
#     the form by itself.
#   - Real submit button: #id_advanced_search ("Search Candidates").
#     Submitting requires clicking this after the keyword is committed
#     as a tag; Enter alone only creates the tag.
_ANY_KEYWORD_INPUT_SELECTOR = "#id_any_keyword"
_SEARCH_SUBMIT_BUTTON_SELECTOR = "#id_advanced_search"

# CONFIRMED (live test): Shine now renders a `<div id="tooltipOverlay"
# class="tooltip-overlay">` covering part of the page — a first-visit
# hint/promo overlay, not a real modal requiring dismissal through any
# particular UI flow. With it present, Playwright's actionability check
# refuses to click #id_advanced_search at all ("intercepts pointer
# events"), retries for the full timeout, and gives up — the search is
# never actually submitted, so the whole crawl cycle silently comes
# back with zero candidates. Removed via a direct DOM removal (not a
# click) before submitting, since it's just an informational overlay
# with no state that depends on being "properly" dismissed.
_TOOLTIP_OVERLAY_SELECTOR = "#tooltipOverlay"


def _is_suid_url(url: str) -> bool:
    return "suid=" in url


@dataclass(frozen=True)
class PageInfo:
    current_page: int
    total_pages: int

    @property
    def has_next(self) -> bool:
        return self.current_page < self.total_pages


def _pagination_bar_text(page) -> str:
    bar = page.query_selector(_PAGINATION_BAR_SELECTOR)
    if bar is None:
        raise ValueError(
            f"Pagination bar ({_PAGINATION_BAR_SELECTOR}) not found on page "
            f"{page.url!r} — Shine's markup may have changed, or this isn't "
            "a search-results page."
        )
    return bar.inner_text()


def extract_page_info(page) -> PageInfo:
    """
    Parses "Page: X of Y" directly out of Shine's own pagination text,
    rather than computing total_pages from the result count — Shine's
    own number is the source of truth and avoids off-by-one errors
    from assumptions about page size.
    """
    match = _PAGE_TEXT_PATTERN.search(_pagination_bar_text(page))
    if not match:
        raise ValueError(
            "Could not parse 'Page: X of Y' out of pagination bar text: "
            f"{_pagination_bar_text(page)!r}"
        )
    current_page, total_pages = match.groups()
    return PageInfo(current_page=int(current_page), total_pages=int(total_pages))


def extract_result_count(page) -> int | None:
    """Total candidate count for the current search, if shown on this page."""
    for selector in _RESULT_COUNT_SELECTORS:
        el = page.query_selector(selector)
        if el is not None:
            text = el.inner_text().strip()
            if text.isdigit():
                return int(text)
    return None


def has_next_page(page) -> bool:
    bar = page.query_selector(_PAGINATION_BAR_SELECTOR)
    if bar is None:
        return False
    arrows = bar.query_selector_all(_PAGINATION_ARROW_SELECTOR)
    if not arrows:
        return False
    next_arrow = arrows[-1]
    classes = next_arrow.get_attribute("class") or ""
    return _DISABLED_ARROW_CLASS not in classes


def _dismiss_tooltip_overlay(page) -> None:
    """
    Removes Shine's `#tooltipOverlay` (a first-visit hint/promo overlay
    that intercepts pointer events) if present, so a subsequent click
    on whatever it's covering actually lands. Called right before
    every click this extractor performs — the overlay has been
    observed appearing before the search-submit click, and there's no
    confirmed guarantee it can't also reappear before a pagination
    click, so this is cheap enough to call every time rather than
    assumed to only matter once per page load. A no-op if the overlay
    isn't present.
    """
    page.evaluate(
        "(selector) => { const el = document.querySelector(selector); "
        "if (el) el.remove(); }",
        _TOOLTIP_OVERLAY_SELECTOR,
    )


def go_to_next_page(page, timeout_ms: int = 30000) -> None:
    """
    Clicks the '>' pagination arrow and waits for the next page to
    actually load. Caller is responsible for checking has_next_page()
    first — clicking a disabled arrow is a no-op on Shine's side but
    would leave the caller waiting on a navigation that never happens.

    CONFIRMED (live test): page.wait_for_load_state("networkidle") -
    used here previously - times out even though the page genuinely
    advances, same class of issue as submit_keyword_search's
    wait_for_url() problem (most likely the aborted image/font/media
    requests in browser.py never resolving Playwright's "idle"
    heuristic). This instead polls the pagination bar's own "Page: X
    of Y" text until the page number actually advances, which is both
    more robust and a more direct confirmation of the thing that
    actually matters.
    """
    bar = page.query_selector(_PAGINATION_BAR_SELECTOR)
    if bar is None:
        raise ValueError("Pagination bar not found; cannot go to next page.")
    arrows = bar.query_selector_all(_PAGINATION_ARROW_SELECTOR)
    if not arrows:
        raise ValueError("No pagination arrows found; cannot go to next page.")

    current_page = extract_page_info(page).current_page
    _dismiss_tooltip_overlay(page)
    arrows[-1].click()

    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        try:
            if extract_page_info(page).current_page != current_page:
                return
        except ValueError:
            pass  # pagination bar momentarily detached mid-navigation
        page.wait_for_timeout(250)

    raise TimeoutError(
        f"Pagination click never advanced past page {current_page} "
        f"within {timeout_ms}ms."
    )


def submit_keyword_search(page, search_term: str, timeout_ms: int = 30000) -> None:
    """
    Fills in and submits the "Any of these keywords" search, waiting
    for Shine to redirect to the resulting "?suid=<hash>" results
    page. Assumes `page` is already on the advanced-search form
    (crawler/tasks.py owns navigation + login-expiry checking, same
    pattern as go_to_next_page() assuming an already-loaded results
    page).

    Confirmed two-step submission: pressing Enter in the keyword field
    only commits it as a tag chip (the form's own jQuery tagger
    widget), it does not submit the form; the actual submit is a click
    on the real #id_advanced_search button. Raises a clear
    playwright.TimeoutError if the "?suid=" redirect never happens
    within timeout_ms, rather than silently treating whatever page is
    currently loaded as search results.
    """
    keyword_input = page.query_selector(_ANY_KEYWORD_INPUT_SELECTOR)
    if keyword_input is None:
        raise ValueError(
            f"Search keyword input ({_ANY_KEYWORD_INPUT_SELECTOR}) not found "
            f"on {page.url!r} — Shine's advanced search form may have changed."
        )

    keyword_input.fill(search_term)
    keyword_input.press("Enter")

    submit_button = page.query_selector(_SEARCH_SUBMIT_BUTTON_SELECTOR)
    if submit_button is None:
        raise ValueError(
            f"Search submit button ({_SEARCH_SUBMIT_BUTTON_SELECTOR}) not "
            f"found on {page.url!r} — Shine's advanced search form may have "
            "changed."
        )
    _dismiss_tooltip_overlay(page)
    submit_button.click()

    # CONFIRMED (live test): page.wait_for_url() raises TimeoutError
    # here even though the navigation genuinely completes (page.url is
    # already correct the moment the exception fires) — regardless of
    # whether it's passed a compiled re.Pattern or a plain callable.
    # wait_for_url() internally waits for the page's "load" event, and
    # with images/media/fonts blocked via route.abort() in browser.py,
    # something on this page apparently never signals load completion
    # the normal way. Polling page.url directly sidesteps Playwright's
    # navigation/load-state machinery entirely and isn't affected by
    # this.
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        if _is_suid_url(page.url):
            break
        page.wait_for_timeout(250)
    else:
        raise TimeoutError(
            f"Search for {search_term!r} never redirected to a '?suid=' "
            f"results page within {timeout_ms}ms (stuck at {page.url!r})."
        )

    # CONFIRMED (live test): the URL reaching "?suid=" only means the
    # server-side redirect happened — Shine still renders the actual
    # candidate list via a separate client-side call afterward (seen
    # live: page body still literally read "Loading..." after the
    # suid redirect had already completed). Treating "suid= is in the
    # URL" as "ready to extract" was the real cause of searches
    # intermittently coming back with zero candidates: the extractor
    # ran before the results had actually rendered, for whichever
    # searches happened to be slower than others that particular time.
    # This waits for either the pagination bar or a candidate card to
    # actually appear before returning. A genuinely empty result set
    # (a real "no candidates match" case) will still just hit this
    # timeout and return anyway rather than raising — that's a
    # legitimate outcome, not a bug — but it's logged so an operator
    # can tell "genuinely zero results" apart from "timed out" if it
    # ever needs checking.
    while time.monotonic() < deadline:
        if page.query_selector(_PAGINATION_BAR_SELECTOR) is not None or (
            page.query_selector(CANDIDATE_CARD_SELECTOR) is not None
        ):
            return
        page.wait_for_timeout(250)

    logger.warning(
        "Search for %r reached a '?suid=' results page but neither the "
        "pagination bar nor any candidate card appeared within %sms — "
        "proceeding anyway; this term may genuinely have zero results, "
        "or the results are just still loading.",
        search_term,
        timeout_ms,
    )


def _candidate_card_elements(page):
    cards = page.query_selector_all(CANDIDATE_CARD_SELECTOR)
    return [
        card
        for card in cards
        if _CANDIDATE_CARD_ID_PATTERN.match(card.get_attribute("id") or "")
    ]


def extract_candidate_card_id(card_element) -> str:
    """
    The "cnd_div_<hash>" id itself, e.g. "cnd_div_50de9bda998b025b737b0980"
    — a stable per-candidate identity straight from the DOM, independent
    of whatever profile URL ends up in the card's markup.
    """
    return card_element.get_attribute("id")


def extract_candidate_card_phone(card_element) -> str | None:
    """
    Reads the phone number straight out of the card's own DOM
    attribute — never clicks anything (per this project's standing
    rule: Shine already exposes it unmasked in the list view, unlike
    JobHai/Naukri/Apna in the reference extension, which genuinely
    need a click). Prefers data-full-mobile; falls back to
    data-masked only if that value itself turns out not to be masked.
    Mirrors content.js's shineCardAdapter() exactly.
    """
    phone_element = card_element.query_selector(_CARD_PHONE_SELECTOR)
    if phone_element is None:
        return None

    for attribute in ("data-full-mobile", "data-masked"):
        raw = phone_element.get_attribute(attribute)
        if not raw:
            continue

        cleaned = re.sub(r"^\+91-?", "", raw)
        cleaned = re.sub(r"[^0-9]", "", cleaned)

        if (
            cleaned
            and not _looks_masked(raw)
            and not _looks_masked(cleaned)
            and not _is_phone_ignored(cleaned)
        ):
            return cleaned

    return None


# CONFIRMED (live test against the real extension backend's own server
# logs): Ollama's structuring returns null for every field except phone
# (which the backend takes straight from our own "phone" field, not
# from Ollama at all) when "elements" is sent as an empty array — even
# though "rawText" is a clean, well-formatted plain-text card. The
# backend's structuring is built around the "elements" array (a
# structured per-element DOM walk: text + attributes + tree position)
# as its real signal, exactly as content.js's own buildPageElementsArray()
# / buildImagesArray() / buildFilesArray() produce it for every other
# portal's sendXCard(). This is that same logic, ported verbatim and
# run live in-page via Playwright's evaluate() (rather than
# hand-translated to Python) so the output is byte-for-byte what the
# real extension would have produced — hand-porting DOM-walking logic
# risks subtle mismatches that are hard to notice until the backend
# silently produces nulls again.
_BUILD_STRUCTURED_DATA_JS = """
(cardElement) => {
    function isElementActuallyVisible(el) {
        if (el.offsetWidth === 0 && el.offsetHeight === 0) return false;
        const cs = window.getComputedStyle(el);
        if (cs.display === "none" || cs.visibility === "hidden" || cs.opacity === "0") return false;
        return true;
    }

    function isInsideBlockedContainer(el) {
        const blockedContainerSelectors = ["header", "nav", "footer", "#wf-status-panel", ".react-pdf__Page__textContent"];
        return blockedContainerSelectors.some(function (selector) {
            return el.closest(selector) !== null;
        });
    }

    function buildElementTreePath(pageElement) {
        const pathNodes = [];
        let current = pageElement;
        while (current && current.tagName && current.tagName.toLowerCase() !== "body") {
            const tagName = current.tagName.toLowerCase();
            const classValue = current.getAttribute("class");
            const classes = classValue ? classValue.split(" ").map(function (c) { return c.trim(); }).filter(function (c) { return c !== ""; }) : [];
            const stableClass = classes.filter(function (c) { return !/\\d/.test(c) && c.length >= 6; }).reduce(function (longest, c) { return c.length > longest.length ? c : longest; }, "");
            let nodeKey;
            if (stableClass) {
                nodeKey = tagName + "." + stableClass;
            } else {
                const parent = current.parentElement;
                const siblings = parent ? Array.from(parent.children).filter(function (c) { return c.tagName === current.tagName; }) : [];
                const index = siblings.indexOf(current);
                nodeKey = siblings.length > 1 ? tagName + "[" + index + "]" : tagName;
            }
            pathNodes.unshift(nodeKey);
            current = current.parentElement;
        }
        return pathNodes.join(" > ");
    }

    function buildElementSiblings(pageElement) {
        const parent = pageElement.parentElement;
        if (!parent) return { prev: null, next: null };
        const children = Array.from(parent.children);
        const selfIndex = children.indexOf(pageElement);
        const prevElement = selfIndex > 0 ? children[selfIndex - 1] : null;
        const nextElement = selfIndex < children.length - 1 ? children[selfIndex + 1] : null;
        function describeSibling(sibling) {
            if (!sibling) return null;
            const tagName = sibling.tagName.toLowerCase();
            const classValue = sibling.getAttribute("class");
            const classes = classValue ? classValue.split(" ").map(function (c) { return c.trim(); }).filter(function (c) { return c !== ""; }) : [];
            const id = sibling.getAttribute("id") || null;
            const directText = Array.from(sibling.childNodes)
                .filter(function (n) { return n.nodeType === Node.TEXT_NODE; })
                .map(function (n) { return n.textContent.trim(); })
                .filter(function (t) { return t !== ""; })
                .join(" ") || null;
            return { tag: tagName, classes: classes, id: id, text: directText };
        }
        return { prev: describeSibling(prevElement), next: describeSibling(nextElement) };
    }

    function collectAriaValues() {
        const allElements = document.querySelectorAll("*");
        const ariaAttributeNames = ["aria-label", "aria-describedby", "aria-labelledby", "aria-placeholder", "aria-roledescription", "aria-valuetext"];
        const allAriaValues = [];
        allElements.forEach(function (pageElement) {
            ariaAttributeNames.forEach(function (ariaAttributeName) {
                const ariaValue = pageElement.getAttribute(ariaAttributeName);
                if (ariaValue) allAriaValues.push(ariaValue.trim().toLowerCase());
            });
        });
        return allAriaValues;
    }

    function isAriaValueUnique(ariaValue, allAriaValues) {
        const normalizedValue = ariaValue.trim().toLowerCase();
        return allAriaValues.filter(function (value) { return value === normalizedValue; }).length === 1;
    }

    function buildElementAttributes(pageElement, allAriaValues) {
        const attributesArray = [];
        const rawAttributes = pageElement.attributes;
        for (let i = 0; i < rawAttributes.length; i++) {
            const currentAttribute = rawAttributes[i];
            const attributeName = currentAttribute.name;
            let attributeValue = currentAttribute.value;
            if (attributeName === "class") {
                attributeValue = currentAttribute.value.split(" ").filter(function (c) { return c.trim() !== ""; });
            }
            const attributeObject = { attributeName: attributeName, value: attributeValue };
            if (attributeName.startsWith("aria-")) {
                attributeObject.isUnique = isAriaValueUnique(currentAttribute.value, allAriaValues);
            }
            attributesArray.push(attributeObject);
        }
        return attributesArray;
    }

    function buildPageElementsArray(rootElement) {
        const allPageElements = rootElement ? rootElement.querySelectorAll("*") : document.querySelectorAll("*");
        const allAriaValues = collectAriaValues();
        const extractedElementsArray = [];
        const treePathCounters = {};
        const blockedTagTypes = new Set(["script", "style", "noscript", "head", "html", "body", "meta", "link", "svg", "path", "br", "hr", "iframe", "canvas", "header", "nav", "footer", "mark", "title"]);
        let globalElementIndex = 0;
        allPageElements.forEach(function (pageElement) {
            if (!blockedTagTypes.has(pageElement.tagName.toLowerCase()) && !isInsideBlockedContainer(pageElement) && isElementActuallyVisible(pageElement)) {
                const directTextContent = Array.from(pageElement.childNodes)
                    .filter(function (n) { return n.nodeType === Node.TEXT_NODE; })
                    .map(function (n) { return n.textContent.trim(); })
                    .filter(function (t) { return t !== ""; })
                    .join(" ");
                if (directTextContent !== "") {
                    const treePath = buildElementTreePath(pageElement);
                    const occurrenceIndex = treePathCounters[treePath] || 0;
                    treePathCounters[treePath] = occurrenceIndex + 1;
                    extractedElementsArray.push({
                        globalIndex: globalElementIndex,
                        tagType: pageElement.tagName.toLowerCase(),
                        text: directTextContent,
                        attributes: buildElementAttributes(pageElement, allAriaValues),
                        treePath: treePath,
                        occurrenceIndex: occurrenceIndex,
                        siblings: buildElementSiblings(pageElement)
                    });
                    globalElementIndex++;
                }
            }
        });
        return extractedElementsArray;
    }

    function buildImagesArray(rootElement) {
        const allImages = rootElement ? rootElement.querySelectorAll("img") : document.querySelectorAll("img");
        const imagesResult = [];
        allImages.forEach(function (imgElement) {
            if (isInsideBlockedContainer(imgElement)) return;
            if (!isElementActuallyVisible(imgElement)) return;
            const src = imgElement.getAttribute("src") || imgElement.getAttribute("data-src") || null;
            if (!src) return;
            imagesResult.push({ src: src, alt: imgElement.getAttribute("alt") || null, treePath: buildElementTreePath(imgElement) });
        });
        return imagesResult;
    }

    function buildFilesArray(rootElement) {
        const allAnchors = rootElement ? rootElement.querySelectorAll("a[href]") : document.querySelectorAll("a[href]");
        const filesResult = [];
        const fileExtensions = [".pdf", ".doc", ".docx", ".txt", ".rtf"];
        const fileKeywords = ["resume", "cv", "download", "attachment"];
        allAnchors.forEach(function (anchorElement) {
            if (isInsideBlockedContainer(anchorElement)) return;
            if (!isElementActuallyVisible(anchorElement)) return;
            const href = anchorElement.getAttribute("href") || "";
            const text = (anchorElement.innerText || "").trim().toLowerCase();
            const isFileExtension = fileExtensions.some(function (ext) { return href.toLowerCase().includes(ext); });
            const isFileKeyword = fileKeywords.some(function (kw) { return text.includes(kw) || href.toLowerCase().includes(kw); });
            if (!isFileExtension && !isFileKeyword) return;
            filesResult.push({ href: href, text: anchorElement.innerText.trim() || null, treePath: buildElementTreePath(anchorElement) });
        });
        return filesResult;
    }

    return {
        elements: buildPageElementsArray(cardElement),
        images: buildImagesArray(cardElement),
        files: buildFilesArray(cardElement)
    };
}
"""


def extract_candidate_cards_html(
    page,
) -> list[tuple[str, str | None, str, list, list, list]]:
    """
    Returns (card_id, phone, raw_text, elements, images, files) for
    every candidate card on the current page, in DOM order.

    CONFIRMED against content.js's own sendShineCard(): what it sends
    as "rawText" is `cardElement.innerText` — the plain visible text of
    the card (name, experience, salary, location, current/previous
    company, education, skills) — NOT the card's outerHTML. The
    backend's LLM structuring is built to parse that plain-text shape;
    handing it raw markup instead (tags, class names, inline styles,
    SVG icon paths) buries the real content in noise the model was
    never prompted to expect. inner_text() is Playwright's equivalent
    of innerText (rendered text only, following visibility/CSS the
    same way the browser does).

    elements/images/files are the structured DOM data _BUILD_STRUCTURED
    _DATA_JS produces — see that constant's docstring for why these
    can't just be left empty.

    card_id and phone are included alongside it since crawler/tasks.py
    needs both for dedup/logging and the pre-submission duplicate
    check, without a second pass over the page.

    Each card is extracted independently, inside its own try/except —
    one card throwing (a detached DOM node, a JS error in the
    structured-data walk) used to abort the entire page: the exception
    propagated out of this whole function, crawler/tasks.py discarded
    every card already extracted from the page (including ones that
    worked fine), retried the identical unchanged DOM (which fails the
    same way again), and after exhausting retries gave up on every
    remaining page of the term, not just this one page. Now a single
    bad card is skipped and logged; every other card on the page is
    still saved.
    """
    results = []
    for card in _candidate_card_elements(page):
        try:
            card_id = extract_candidate_card_id(card)
            phone = extract_candidate_card_phone(card)
            raw_text = card.inner_text()
            structured = card.evaluate(_BUILD_STRUCTURED_DATA_JS)
        except Exception:
            logger.warning(
                "Skipping one candidate card that failed to extract "
                "(page otherwise continues normally)",
                exc_info=True,
            )
            continue

        results.append(
            (
                card_id,
                phone,
                raw_text,
                structured["elements"],
                structured["images"],
                structured["files"],
            )
        )
    return results

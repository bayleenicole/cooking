#!/usr/bin/env python3
"""Rewrite Start Cooking cards for recipes edited in the recipe planner.

Runs in GitHub Actions (.github/workflows/rebuild-cards.yml) whenever the
planner publishes data/edits.json. The planner publishes quick rule-built
cards (cook.by == "rules"); this script asks Claude to write proper ones in
the same style as the original hand-written cards, then saves them back into
data/edits.json with cook.by == "claude".

Needs the repository secret ANTHROPIC_API_KEY. Without it, it does nothing.
"""
import json, os, re, subprocess, sys, time, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDITS = os.path.join(ROOT, "data", "edits.json")
KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MODEL = os.environ.get("CARD_MODEL", "").strip() or "claude-sonnet-5-5"

RULES = """You write the step cards for a recipe's "Start Cooking" page: a phone slideshow someone follows while cooking.
Write them exactly in the style of the examples. Rules:
- One action in one place per card; merge small steps that happen together. Usually 3-6 cards, never more than 10.
- Every wait (bake, simmer, rest, chill, rise, marinate) gets its own card with timer_seconds = the low end of the stated time (40-50 minutes -> 2400). Only add a timer when the recipe states a time of a minute or more. Never invent a time.
- When something can be done during a wait, add a short "while" note to that waiting card (e.g. "While this bakes, make the dressing") and order the cards that way.
- If the recipe uses the oven, a card must say to preheat it (in the first card, or while dough rises).
- Titles are 2-4 words, Title Case, naming the action ("Sear the Chicken", "Make the Dressing", "Bake"). Recipes in parts put the part first: "Dressing: Blend".
- Text is 1-2 sentences and keeps the recipe's own wording and amounts; don't add facts, don't drop amounts.
- uses lists what that card needs: ingredient numbers from the numbered list, or a short custom line for a split amount (e.g. "1 tbsp olive oil" when only part of a divided ingredient is used). Every numbered ingredient must appear on at least one card, except lines marked (from above).
- Do not list section headings in uses. Do not write a "Gather Ingredients" card; it is added automatically.
- Never refer to step or card numbers ("step 7"); the cook can't see them. Name the thing instead ("make the frosting").
- Brand names are already removed from the text you are given; don't add any back."""

TOOL = {
    "name": "save_cards",
    "description": "Save the finished Start Cooking cards for this recipe.",
    "input_schema": {
        "type": "object",
        "properties": {"cards": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "text": {"type": "string"},
                "timer_seconds": {"type": "integer"},
                "while": {"type": "string"},
                "uses": {"type": "array", "items": {"anyOf": [{"type": "integer"}, {"type": "string"}]}},
            },
            "required": ["title", "text"]}}},
        "required": ["cards"]},
}


def strip_tag(line):
    return re.sub(r"\s*\((?:divided|optional)\)\s*$", "", line.strip(), flags=re.I)


def numbered(ings):
    out, idx = [], {}
    for k, line in enumerate(ings):
        if line.strip().endswith(":"):
            out.append(f"   {line.strip()}")
        else:
            out.append(f"{k}. {line.strip()}")
            idx[strip_tag(line).lower()] = k
    return "\n".join(out), idx


def steps_text(parts):
    out = []
    for p in parts:
        if p.get("part"):
            out.append(p["part"])
        out += [f"- {s}" for s in p["steps"]]
    return "\n".join(out)


def example_block(ex):
    listing, idx = numbered(ex["ingredients"])
    cards = []
    for c in ex["cards"]:
        d = {"title": c["title"], "text": c["text"]}
        if c.get("timer"): d["timer_seconds"] = c["timer"]
        if c.get("while"): d["while"] = c["while"]
        if c.get("uses"):
            d["uses"] = [idx.get(strip_tag(u).lower(), u) for u in c["uses"]]
        cards.append(d)
    return (f"<example>\nRecipe: {ex['title']}\nIngredients:\n{listing}\nSteps:\n{steps_text(ex['steps'])}\n"
            f"Cards: {json.dumps(cards, ensure_ascii=False)}\n</example>")


def old_cards(slug):
    """The page's current hand-written cards, so unchanged steps keep their cards."""
    try:
        h = open(os.path.join(ROOT, slug, "index.html"), encoding="utf-8").read()
        i = h.index("let R = ") + len("let R = ")
        data, _ = json.JSONDecoder().raw_decode(h[i:])
        return data, [s for s in data["slides"] if not s.get("gather")]
    except Exception:
        return {}, []


def ask_claude(prompt):
    body = json.dumps({"model": MODEL, "max_tokens": 4000, "system": RULES,
                       "tools": [TOOL], "tool_choice": {"type": "auto"},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST",
                                 headers={"x-api-key": KEY, "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                res = json.load(r)
            for block in res.get("content", []):
                if block.get("type") == "tool_use":
                    return block["input"]["cards"]
            text = "".join(b.get("text", "") for b in res.get("content", []))
            m = re.search(r"\{[\s\S]*\}|\[[\s\S]*\]", text)      # answered in plain JSON instead
            if m:
                got = json.loads(m.group(0))
                return got["cards"] if isinstance(got, dict) else got
            raise RuntimeError("no cards in reply")
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:300]
            if e.code in (429, 500, 529) and attempt < 2:
                time.sleep(20 * (attempt + 1)); continue
            raise RuntimeError(f"API {e.code}: {msg}")


def check(cards, ings):
    problems = []
    needed = {k for k, l in enumerate(ings)
              if l.strip() and not l.strip().endswith(":")
              and not re.search(r"\(\s*from (above|the [a-z ]+)\s*\)|:\s*all$", l, re.I)}
    used = {u for c in cards for u in c.get("uses", []) if isinstance(u, int)}
    texts = " ".join(str(u) for c in cards for u in c.get("uses", []) if isinstance(u, str)).lower()
    missing = [k for k in needed - used if strip_tag(ings[k]).lower()[:12] not in texts]
    if missing:
        problems.append("These ingredients are on no card: " + "; ".join(f"{k}. {ings[k]}" for k in sorted(missing)))
    bad = [u for c in cards for u in c.get("uses", []) if isinstance(u, int) and not (0 <= u < len(ings)) ]
    if bad:
        problems.append(f"Ingredient numbers that don't exist: {bad}")
    for c in cards:
        if not c.get("text", "").strip():
            problems.append(f"Card '{c.get('title')}' has no text")
        if len(c.get("title", "").split()) > 6:
            problems.append(f"Title too long: {c.get('title')}")
    if not 1 <= len(cards) <= 12:
        problems.append(f"{len(cards)} cards")
    return problems


# ---- Gather Ingredients cards: same rules as cook/build_cook.py ----
LONG_WAIT = re.compile(r"overnight|several hours|few hours|at least \d+ hours", re.I)
is_long = lambda s: (s.get("timer") or 0) >= 3600 or bool(LONG_WAIT.search(s["text"]))
cap1 = lambda s: s[:1].upper() + s[1:]


def with_gather(ings, slides):
    ings = [x for x in ings if x and x.strip()]
    phase_of, p = [], 0
    for s in slides:
        phase_of.append(p)
        if is_long(s): p += 1
    nph = p + (0 if slides and is_long(slides[-1]) else 1)
    first = {}
    for i, s in enumerate(slides):
        for u in s.get("uses", []):
            first.setdefault(strip_tag(u).lower(), phase_of[i])
    groups = [[] for _ in range(max(1, nph))]
    section = None
    for line in ings:
        if line.strip().endswith(":"):
            section = line; continue
        if re.search(r"\(\s*from (above|the [a-z ]+)\s*\)|:\s*all$", line, re.I):
            continue
        k = strip_tag(line).lower()
        ph = first.get(k)
        if ph is None:
            ph = next((v for u, v in first.items() if u.startswith(k[:14]) or k.startswith(u[:14])), 0)
        ph = min(ph, len(groups) - 1)
        if section and (not groups[ph] or groups[ph][-1][0] != section):
            groups[ph].append((section, None))
        groups[ph].append((section, line))

    def gather(items, later):
        return {"title": "Gather Ingredients", "gather": True,
                "text": "While you wait, get these out for the next part" if later else "Get everything out before you start",
                "uses": [("§" + cap1(re.sub(r"^For the\s+|:\s*$", "", sec, flags=re.I).strip())) if ln is None else ln
                         for sec, ln in items]}
    if len(groups) == 1 or not any(groups[1:]):
        return [gather([x for g in groups for x in g], False)] + slides
    out, g = [gather(groups[0], False)], 1
    for s in slides:
        out.append(s)
        if is_long(s) and g < len(groups):
            if groups[g]: out.append(gather(groups[g], True))
            g += 1
    return out


def build(slug, cook, examples):
    ings = cook.get("ingredients") or []
    parts = cook.get("steps") or []
    listing, _ = numbered(ings)
    page, before = old_cards(slug)
    prompt = "\n\n".join(example_block(e) for e in examples)
    prompt += (f"\n\nNow write the cards for this recipe, which was just edited.\n"
               f"Recipe: {cook.get('title') or page.get('title', '')}\n"
               f"Ingredients:\n{listing}\nSteps:\n{steps_text(parts)}\n")
    if before:
        prompt += ("\nCards from before the edit. Keep any card whose step didn't change, word for word, "
                   "and rewrite only what the edit changed:\n" + json.dumps(before, ensure_ascii=False) + "\n")
    prompt += "\nCall save_cards with the cards."
    cards = ask_claude(prompt)
    problems = check(cards, ings)
    if problems:
        cards = ask_claude(prompt + "\n\nYour last attempt had these problems, fix them:\n- " + "\n- ".join(problems)
                           + "\nLast attempt: " + json.dumps(cards, ensure_ascii=False))
        problems = check(cards, ings)
        if problems:
            raise RuntimeError("; ".join(problems))
    slides = []
    for c in cards:
        s = {"title": c["title"].strip(), "text": c["text"].strip()}
        t = c.get("timer_seconds")
        if isinstance(t, int) and t >= 60: s["timer"] = t
        if c.get("while"): s["while"] = c["while"].strip()
        uses = [strip_tag(ings[u]) if isinstance(u, int) else str(u).strip() for u in c.get("uses", [])]
        if uses: s["uses"] = uses
        slides.append(s)
    return with_gather(ings, slides)


def git(*a):
    return subprocess.run(["git", "-C", ROOT, *a], check=True, capture_output=True, text=True).stdout


def main():
    if not KEY:
        print("ANTHROPIC_API_KEY is not set; leaving the quick cards in place."); return
    data = json.load(open(EDITS, encoding="utf-8"))
    todo = {ck: o["cook"] for ck, o in (data.get("recipes") or {}).items()
            if o and isinstance(o.get("cook"), dict) and o["cook"].get("slides") and o["cook"].get("by") != "claude"}
    if not todo:
        print("No recipes need new cards."); return
    examples = json.load(open(os.path.join(ROOT, "tools", "card_examples.json"), encoding="utf-8"))
    done, failed = {}, []
    for ck, cook in todo.items():
        try:
            done[ck] = (cook.get("src"), build(ck, cook, examples))
            print(f"wrote cards for {ck}: {len(done[ck][1])} cards")
        except Exception as e:
            failed.append(ck); print(f"!! {ck}: {e}")
    if not done:
        sys.exit(1 if failed else 0)
    for attempt in range(4):
        git("pull", "--rebase", "--quiet")
        data = json.load(open(EDITS, encoding="utf-8"))
        changed = 0
        for ck, (src, slides) in done.items():
            o = (data.get("recipes") or {}).get(ck)
            c = o and o.get("cook")
            if c and c.get("by") != "claude" and c.get("src") == src:   # recipe not edited again meanwhile
                c["slides"], c["by"] = slides, "claude"; changed += 1
        if not changed:
            print("Nothing to save (recipes were edited again; the next run handles them)."); return
        with open(EDITS, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
        git("add", EDITS)
        git("commit", "-q", "-m", f"Start Cooking cards written by Claude ({changed} recipe{'s' if changed != 1 else ''})")
        try:
            git("push", "-q"); print(f"saved {changed}"); break
        except subprocess.CalledProcessError as e:
            print("push failed, retrying:", (e.stderr or "").strip()[:200])
            git("reset", "-q", "--hard", "HEAD~1"); time.sleep(5)
    else:
        print("!! couldn't save the cards after 4 tries"); sys.exit(1)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

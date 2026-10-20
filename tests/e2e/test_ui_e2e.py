"""Suite E2E large (pytest-playwright) — teste l'interface complète : filtres,
recherche, matières, plan, budget, cocha ge, chat, résumé IA, menu mobile,
déconnexion et écrans d'auth.

Isolement : chaque test monte son propre serveur + sa propre DB seedée
(fixture pb_server fonction-scope) → aucune pollution entre tests.

Contrat du backend : GET /api/items renvoie les devoirs non faits (retard +
à venir) ET les faits (fix include_done) pour que l'UI « Faits » fonctionne.
À l'arrivée : 5 cartes, late=1, soon=3, done=1.

Fragilité connue : après la navigation, les items se chargent de façon
asynchrone (résumés LLM). Chaque test attend donc que les cartes soient là
avant d'interagir.
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from .conftest import TOKEN

pytestmark = pytest.mark.e2e


def _go(page, base: str, ready: bool = True) -> None:
    page.add_init_script(f"sessionStorage.setItem('pronote_token', '{TOKEN}')")
    page.goto(base + "/", wait_until="networkidle", timeout=45000)
    if ready:
        page.wait_for_function(
            "() => document.querySelectorAll('.document-card').length >= 1",
            timeout=20000,
        )


def _go_guest(page, base: str) -> None:
    page.goto(base + "/", wait_until="networkidle", timeout=45000)


# ── session & rendu de base ────────────────────────────────────────────


def test_session_active(page, pb_server):
    _go(page, pb_server)
    expect(page.locator("body.is-guest")).to_have_count(0)
    expect(page.locator(".document-card")).to_have_count(5)


def test_counters_nav(page, pb_server):
    _go(page, pb_server)
    expect(page.locator("#navLate")).to_have_text("1")
    expect(page.locator("#navSoon")).to_have_text("3")
    expect(page.locator("#navDone")).to_have_text("1")
    expect(page.locator("#navAll")).to_have_text("5")


# ── filtres ────────────────────────────────────────────────────────────
def test_filter_late(page, pb_server):
    _go(page, pb_server)
    page.locator('.nav-item[data-filter="late"]').click()
    expect(page.locator("#lateSection")).to_be_visible()
    expect(page.locator("#lateList .document-card")).to_have_count(1)
    expect(page.locator("#lateList .document-card").first).to_contain_text("Maths")
    expect(page.locator("#soonList")).to_be_hidden()
    expect(page.locator("#doneList")).to_be_hidden()


def test_filter_soon(page, pb_server):
    _go(page, pb_server)
    page.locator('.nav-item[data-filter="soon"]').click()
    expect(page.locator("#soonSection")).to_be_visible()
    expect(page.locator("#soonList .document-card")).to_have_count(3)
    expect(page.locator("#soonList .document-card").first).to_contain_text("Physique")
    expect(page.locator("#lateList")).to_be_hidden()
    expect(page.locator("#doneList")).to_be_hidden()


def test_filter_done(page, pb_server):
    _go(page, pb_server)
    page.locator('.nav-item[data-filter="done"]').click()
    expect(page.locator("#doneSection")).to_be_visible()
    expect(page.locator("#doneList .document-card")).to_have_count(1)
    expect(page.locator("#doneList .document-card").first).to_contain_text("Histoire")
    expect(page.locator("#lateList")).to_be_hidden()
    expect(page.locator("#soonList")).to_be_hidden()


def test_filter_all(page, pb_server):
    _go(page, pb_server)
    page.locator('.nav-item[data-filter="done"]').click()
    page.locator('.nav-item[data-filter="all"]').click()
    expect(page.locator("#lateList")).to_be_visible()
    expect(page.locator("#soonList")).to_be_visible()
    expect(page.locator("#doneList")).to_be_visible()
    expect(page.locator(".document-card")).to_have_count(5)


# ── recherche & matière ────────────────────────────────────────────────


def test_search_filters(page, pb_server):
    _go(page, pb_server)
    page.locator("#searchInput").fill("physique")
    page.wait_for_timeout(600)
    expect(page.locator(".document-card")).to_have_count(1)
    expect(page.locator(".document-card").first).to_contain_text("Physique")


def test_subject_filter(page, pb_server):
    _go(page, pb_server)
    page.locator("#subjectSelect").select_option(label="Maths")
    expect(page.locator(".document-card")).to_have_count(1)
    expect(page.locator(".document-card").first).to_contain_text("Maths")


def test_clear_filters(page, pb_server):
    _go(page, pb_server)
    # une recherche sans résultat fait apparaître le bouton « Effacer »
    page.locator("#searchInput").fill("zzzz-inexistant")
    page.wait_for_timeout(600)
    expect(page.locator("#noResults")).to_be_visible()
    page.locator("#clearFilters").click()
    expect(page.locator("#noResults")).to_be_hidden()
    expect(page.locator(".document-card")).to_have_count(5)


# ── cocha ge de devoir / « Faits » ─────────────────────────────────────


def test_mark_done_toggle(page, pb_server):
    _go(page, pb_server)
    expect(page.locator(".document-card.is-done")).to_have_count(1)  # k4
    page.locator('.document-card:not(.is-done)').first.locator(".open-doc").click()
    expect(page.locator(".document-card.is-done")).to_have_count(2)
    page.locator(".document-card.is-done").first.locator(".open-doc").click()
    expect(page.locator(".document-card.is-done")).to_have_count(1)


def test_done_filter_lists_checked(page, pb_server):
    _go(page, pb_server)
    page.locator('.nav-item[data-filter="done"]').click()
    expect(page.locator("#doneSection")).to_be_visible()
    expect(page.locator("#doneList .document-card")).to_have_count(1)
    # on coche un autre devoir -> il rejoint « Faits »
    page.locator('.nav-item[data-filter="all"]').click()
    page.locator('.document-card:not(.is-done)').first.locator(".open-doc").click()
    page.locator('.nav-item[data-filter="done"]').click()
    expect(page.locator("#doneList .document-card")).to_have_count(2)


# ── plan de rattrapage ─────────────────────────────────────────────────


def test_plan_renders(page, pb_server):
    _go(page, pb_server)
    page.locator("#planNav").click()
    expect(page.locator("#planView")).to_be_visible()
    expect(page.locator(".plan-row").first).to_be_visible()


def test_plan_check_toggle(page, pb_server):
    _go(page, pb_server)
    page.locator("#planNav").click()
    before = page.locator(".plan-row").count()
    assert before >= 1
    page.locator(".plan-check").first.click()
    # cocher un devoir l'enlève du plan (il est fait)
    expect(page.locator(".plan-row")).to_have_count(before - 1)


def test_budget_change_replans(page, pb_server):
    _go(page, pb_server)
    page.locator("#planNav").click()
    page.locator("#planBudget").fill("180")
    page.locator("#planBudget").dispatch_event("change")
    page.wait_for_timeout(600)
    expect(page.locator(".plan-row").first).to_be_visible()


def test_replan_button(page, pb_server):
    _go(page, pb_server)
    page.locator("#planNav").click()
    page.locator("#replanBtn").click()
    expect(page.locator(".plan-row").first).to_be_visible()


# ── chat & résumé IA ───────────────────────────────────────────────────


def test_click_card_opens_chat(page, pb_server):
    _go(page, pb_server)
    page.locator(".document-card").first.click()
    expect(page.locator("#chat:not(.hidden)")).to_have_count(1)
    assert "Aide-moi avec" in page.locator("#chatInput").input_value()


def test_chat_send_replies(page, pb_server):
    _go(page, pb_server)
    page.locator("#chatInput").fill("qu'est-ce que je dois faire ?")
    page.locator("#chatForm button[type=submit]").click()
    expect(page.locator(".msg-answer").first).to_be_visible()
    expect(page.locator(".msg-answer").last).not_to_have_text("…")


def test_ai_summary_real_answer(page, pb_server, has_llm):
    if not has_llm:
        pytest.skip("aucun LLM configuré")
    _go(page, pb_server)
    box = page.locator("#aiSummary")
    btn = page.locator("#aiSummaryBtn")
    ok = False
    for _ in range(2):
        btn.click()
        try:
            expect(box).not_to_have_text("", timeout=20000)
            ok = True
            break
        except AssertionError:
            pass
    if not ok:
        pytest.fail(f"résumé IA vide — /api/chat a répondu {box.inner_text()!r}")
    text = box.inner_text()
    assert text.strip() and not re.search(r"Je n'ai pas", text)


# ── menu mobile ────────────────────────────────────────────────────────


def test_mobile_menu_toggle(page, pb_server):
    page.set_viewport_size({"width": 400, "height": 800})
    _go(page, pb_server)
    menu = page.locator("#menuBtn")
    expect(menu).to_be_visible()
    menu.click()
    expect(page.locator(".sidebar")).to_have_class(re.compile(r"open"))
    # refermer : clic sur l'overlay (appui hors menu), comme en usage réel
    page.locator("#navOverlay").click(position={"x": 350, "y": 400})
    expect(page.locator(".sidebar")).not_to_have_class(re.compile(r"open"))


# ── déconnexion ────────────────────────────────────────────────────────


def test_logout_returns_to_guest(page, pb_server):
    _go(page, pb_server)
    page.locator("#logoutBtn").click()
    expect(page.locator("body.is-guest")).to_have_count(1)
    expect(page.locator("#authQr")).to_be_visible()
    expect(page.locator("#aiSummary")).to_be_hidden()
    expect(page.locator("#aiSummaryBtn")).to_be_hidden()


# ── écrans d'authentification ──────────────────────────────────────────


def test_auth_tabs_switch(page, pb_server):
    _go_guest(page, pb_server)
    page.locator('.auth-tab[data-tab="pwd"]').click()
    expect(page.locator("#pwdTab")).to_be_visible()
    expect(page.locator("#qrTab")).to_be_hidden()
    page.locator('.auth-tab[data-tab="qr"]').click()
    expect(page.locator("#qrTab")).to_be_visible()
    expect(page.locator("#pwdTab")).to_be_hidden()

def test_auth_default_tab_desktop(page, pb_server):
    _go_guest(page, pb_server)
    expect(page.locator("#qrTab")).to_be_visible()
    expect(page.locator("#pwdTab")).to_be_hidden()


def test_auth_default_tab_mobile(page, pb_server):
    page.set_viewport_size({"width": 400, "height": 800})
    _go_guest(page, pb_server)
    expect(page.locator("#pwdTab")).to_be_visible()
    expect(page.locator("#qrTab")).to_be_hidden()

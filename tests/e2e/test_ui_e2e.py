"""Suite E2E large (pytest-playwright) — teste l'interface complète : filtres,
recherche, matières, plan, budget, cocha ge, chat, résumé IA, menu mobile,
déconnexion et écrans d'auth. Chaque test repart d'une page neuve (même
serveur isolé + même session seedée).

Seed : k1 Maths en retard, k2 Physique à venir, k3 Anglais sans date,
k4 Histoire fait, k5 SVT sans date → late=1, soon=3, done=1.
"""
from __future__ import annotations

import re
import time

import pytest
from playwright.sync_api import expect

from .conftest import TOKEN

pytestmark = pytest.mark.e2e


def _go(page, base: str) -> None:
    page.add_init_script(f"sessionStorage.setItem('pronote_token', '{TOKEN}')")
    page.goto(base + "/", wait_until="networkidle", timeout=45000)


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
    expect(page.locator("#soonSection")).to_be_hidden()
    expect(page.locator("#lateSection .document-card")).to_have_count(1)
    expect(page.locator("#lateSection .document-card").first).to_contain_text("Maths")


def test_filter_soon(page, pb_server):
    _go(page, pb_server)
    page.locator('.nav-item[data-filter="soon"]').click()
    expect(page.locator("#soonSection")).to_be_visible()
    expect(page.locator("#lateSection")).to_be_hidden()
    expect(page.locator("#soonSection .document-card")).to_have_count(3)
    expect(page.locator("#soonSection .document-card").first).to_contain_text("Physique")


def test_filter_done(page, pb_server):
    _go(page, pb_server)
    page.locator('.nav-item[data-filter="done"]').click()
    expect(page.locator("#doneSection")).to_be_visible()
    expect(page.locator("#doneSection .document-card")).to_have_count(1)
    expect(page.locator("#doneSection .document-card").first).to_contain_text("Histoire")


def test_filter_all(page, pb_server):
    _go(page, pb_server)
    page.locator('.nav-item[data-filter="done"]').click()
    page.locator('.nav-item[data-filter="all"]').click()
    expect(page.locator("#lateSection")).to_be_visible()
    expect(page.locator("#soonSection")).to_be_visible()
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
    page.locator("#subjectSelect").select_option(label="Maths")
    expect(page.locator(".document-card")).to_have_count(1)
    page.locator("#clearFilters").click()
    expect(page.locator(".document-card")).to_have_count(5)


# ── cocha ge de devoir ────────────────────────────────────────────────


def test_mark_done_toggle(page, pb_server):
    _go(page, pb_server)
    card = page.locator('.document-card:not(.is-done)').first
    card.locator(".open-doc").click()
    expect(page.locator(".document-card.is-done")).to_have_count(2)  # k4 + k1
    page.locator(".document-card.is-done").first.locator(".open-doc").click()
    expect(page.locator(".document-card.is-done")).to_have_count(1)


# ── plan de rattrapage ─────────────────────────────────────────────────


def test_plan_renders(page, pb_server):
    _go(page, pb_server)
    page.locator("#planNav").click()
    expect(page.locator("#planView")).to_be_visible()
    expect(page.locator(".plan-row").first).to_be_visible()


def test_plan_check_toggle(page, pb_server):
    _go(page, pb_server)
    page.locator("#planNav").click()
    page.locator(".plan-check").first.click()
    expect(page.locator(".plan-check.active")).to_have_count(1)


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
    page.locator("#aiSummaryBtn").click()
    page.wait_for_timeout(6000)
    text = page.locator("#aiSummary").inner_text()
    assert text.strip() and not re.search(r"Je n'ai pas", text)


# ── menu mobile ────────────────────────────────────────────────────────


def test_mobile_menu_toggle(page, pb_server):
    page.set_viewport_size({"width": 400, "height": 800})
    _go(page, pb_server)
    page.locator("#menuBtn").click()
    expect(page.locator("#nav")).to_have_class(re.compile(r"open"))
    page.locator("#menuBtn").click()
    expect(page.locator("#nav")).not_to_have_class(re.compile(r"open"))


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


# ── santé console ──────────────────────────────────────────────────────


def test_no_console_errors(page, pb_server):
    errors = []
    page.on("console", lambda m: errors.append(m.text()) if m.type() == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    _go(page, pb_server)
    time.sleep(2)
    assert errors == []

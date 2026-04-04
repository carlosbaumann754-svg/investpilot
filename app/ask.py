# app/ask.py — Q&A Chat: Beantwortet Fragen zum Bot mit Claude API
"""
Sammelt relevante Daten (Trades, Decisions, Portfolio, Brain) und
schickt sie als Context an Claude Haiku für natürliche Antworten.
"""

import json
import logging
import os
from datetime import datetime

log = logging.getLogger("investpilot")


def build_context(question, trade_history=None, decision_log=None,
                  brain_state=None, portfolio=None, risk_state=None,
                  scanner_state=None):
    """Baue einen kompakten Context aus den relevanten Daten."""
    parts = []

    # Portfolio-Zusammenfassung
    if portfolio:
        parts.append(f"=== PORTFOLIO (aktuell) ===\n"
                     f"Gesamtwert: ${portfolio.get('total_value', 0):,.2f}\n"
                     f"Cash: ${portfolio.get('credit', 0):,.2f}\n"
                     f"Investiert: ${portfolio.get('invested', 0):,.2f}\n"
                     f"P/L: ${portfolio.get('unrealized_pnl', 0):,.2f}\n"
                     f"Positionen: {portfolio.get('num_positions', 0)}")

        positions = portfolio.get("positions", [])
        if positions:
            pos_lines = []
            for p in positions[:20]:
                symbol = p.get("symbol", p.get("instrument_id", "?"))
                pnl = p.get("pnl", 0)
                pnl_pct = p.get("pnl_pct", 0)
                invested = p.get("invested", 0)
                pos_lines.append(f"  {symbol}: ${invested:,.0f} -> P/L: ${pnl:+,.2f} ({pnl_pct:+.1f}%)")
            parts.append("Positionen:\n" + "\n".join(pos_lines))

    # Brain-Status
    if brain_state:
        parts.append(f"=== BRAIN STATUS ===\n"
                     f"Regime: {brain_state.get('market_regime', 'unknown')}\n"
                     f"Win-Rate: {brain_state.get('win_rate', 0)}%\n"
                     f"Sharpe: {brain_state.get('sharpe_estimate', 0)}\n"
                     f"Zyklen: {brain_state.get('total_runs', 0)}\n"
                     f"Aktive Regeln: {len(brain_state.get('learned_rules', []))}")

        # Instrument-Scores
        scores = brain_state.get("instrument_scores", {})
        if scores:
            score_lines = []
            for iid, data in sorted(scores.items(), key=lambda x: x[1].get("score", 0), reverse=True)[:10]:
                score_lines.append(f"  #{iid}: Score={data.get('score', 0):.1f}, "
                                   f"Win={data.get('consistency', 0):.0f}%")
            parts.append("Top Instrument-Scores:\n" + "\n".join(score_lines))

    # Risk-State
    if risk_state:
        parts.append(f"=== RISK STATUS ===\n"
                     f"Tages-P/L: {risk_state.get('daily_pnl_pct', 0):+.2f}%\n"
                     f"Wochen-P/L: {risk_state.get('weekly_pnl_pct', 0):+.2f}%\n"
                     f"Margin-Puffer: {risk_state.get('margin_buffer_pct', 'N/A')}%")

    # Letzte Trades (max 20)
    if trade_history:
        recent = trade_history[-20:]
        parts.append("=== LETZTE TRADES ===")
        for t in recent:
            ts = t.get("timestamp", "?")[:16]
            action = t.get("action", "?")
            symbol = t.get("symbol", "?")
            amount = t.get("amount_usd", 0)
            status = t.get("status", "?")
            lev = t.get("leverage", 1)
            line = f"  {ts} {action} {symbol} ${amount:,.0f} x{lev} [{status}]"
            parts.append(line)

    # Decision Log (max 15 letzte Entscheidungen)
    if decision_log:
        recent_decisions = decision_log[-15:]
        parts.append("=== TRADE-ENTSCHEIDUNGEN ===")
        for d in recent_decisions:
            ts = d.get("timestamp", "?")[:16]
            action = d.get("action", "?")
            symbol = d.get("symbol", "?")
            regime = d.get("market_regime", "?")
            vix = d.get("vix", "?")
            score = d.get("instrument_score", {})
            parts.append(f"  {ts} {action} {symbol} | Regime={regime}, VIX={vix}, Score={score}")

    # Scanner-Ergebnisse
    if scanner_state:
        results = scanner_state.get("last_results", [])[:10]
        if results:
            parts.append("=== SCANNER TOP-10 ===")
            for r in results:
                parts.append(f"  {r.get('symbol', '?')}: Score={r.get('score', 0):.1f}, "
                             f"Signal={r.get('signal', '?')}, Klasse={r.get('class', '?')}")

    return "\n\n".join(parts)


def ask_question(question, context_data, config=None):
    """Stelle eine Frage an Claude mit dem Bot-Kontext."""
    try:
        import anthropic
    except ImportError:
        return {"error": "anthropic SDK nicht installiert. Bitte 'pip install anthropic' ausführen."}

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        cfg = config or {}
        api_key = cfg.get("ask", {}).get("api_key", "")

    if not api_key:
        return {"error": "ANTHROPIC_API_KEY nicht konfiguriert. Bitte als Umgebungsvariable setzen."}

    context = build_context(question, **context_data)

    system_prompt = (
        "Du bist der InvestPilot Trading Bot Assistent. "
        "Du beantwortest Fragen über den Bot, seine Trades, Performance und Entscheidungen. "
        "Antworte auf Deutsch, kurz und präzise. "
        "Nutze die bereitgestellten Daten um faktenbasierte Antworten zu geben. "
        "Wenn du etwas nicht aus den Daten ableiten kannst, sage das ehrlich. "
        "Verwende Zahlen und konkrete Werte aus den Daten."
    )

    cfg = config or {}
    ask_cfg = cfg.get("ask", {})
    model = ask_cfg.get("model", "claude-sonnet-4-6")
    max_tokens = ask_cfg.get("max_tokens", 1024)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[
                {"role": "user", "content": f"Bot-Daten:\n{context}\n\nFrage: {question}"}
            ],
        )
        answer = response.content[0].text
        return {
            "answer": answer,
            "model": response.model,
            "tokens_used": response.usage.input_tokens + response.usage.output_tokens,
        }
    except Exception as e:
        log.error(f"Ask-Fehler: {e}")
        return {"error": f"Claude API Fehler: {str(e)}"}

#!/usr/bin/env bash
# publicar.sh — publica el trabajo actual en main Y desarrollo del agente NEA.
#
# POR QUÉ EXISTE:
#   Railway despliega desde `main`, pero el trabajo de QA (guardas G4-G13, app/guards.py)
#   vive solo en `desarrollo`. Un fix que solo va a main deja a desarrollo atrás y, al
#   día siguiente, cualquier intento de sincronizar conflictúa en app/turn.py.
#
#   Medido: el cherry-pick de un commit de main sobre desarrollo CONFLICTÚA (UU turn.py);
#   el merge main→desarrollo es limpio y conserva las guardas. Por eso este script
#   usa merge, nunca cherry-pick.
#
# QUÉ HACE:
#   1. commitea (o usa lo ya commiteado) en main
#   2. push a main  → es lo que despliega Railway
#   3. merge main → desarrollo y push → desarrollo recibe los fixes, conserva las guardas
#   4. main NUNCA recibe las guardas (siguen siendo exclusivas de desarrollo)
#
# USO:
#   scripts/publicar.sh                      # commitea lo staged + publica
#   scripts/publicar.sh -m "fix(x): mensaje" # commitea todo con ese mensaje y publica
#   scripts/publicar.sh --no-commit          # publica lo ya commiteado, sin commitear
#
# SEGURIDAD:
#   - Nunca usa --force: si un push es rechazado (alguien empujó), PARA y avisa.
#   - Si el merge main→desarrollo conflictúa, NO commitea nada y sale con error.
#   - Verifica que desarrollo acabe conteniendo todo main antes de dar éxito.

set -euo pipefail

REPO="${NEA_REPO:-$HOME/HermesProyectos/nea-agent}"
cd "$REPO"

VERDE=$'\033[32m'; ROJO=$'\033[31m'; AMAR=$'\033[33m'; FIN=$'\033[0m'
ok()   { echo "${VERDE}✓${FIN} $*"; }
err()  { echo "${ROJO}✗${FIN} $*" >&2; }
avisa(){ echo "${AMAR}!${FIN} $*"; }

MENSAJE=""
NO_COMMIT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -m|--mensaje) MENSAJE="${2:-}"; shift 2 ;;
    --no-commit)  NO_COMMIT=1; shift ;;
    *) err "argumento desconocido: $1"; exit 2 ;;
  esac
done

echo "── publicar: main + desarrollo ──────────────────────────────"

# ── 0. estado limpio y rama correcta ────────────────────────────────────────
RAMA=$(git branch --show-current)
if [[ "$RAMA" != "main" ]]; then
  avisa "estás en '$RAMA'; cambio a main"
  if [[ -n "$(git status --porcelain)" ]]; then
    err "hay cambios sin commitear y no estás en main — commitea o guarda antes"
    exit 1
  fi
  git checkout -q main
fi

git fetch -q origin

# ── 1. commit en main (si hace falta) ────────────────────────────────────────
if [[ "$NO_COMMIT" -eq 0 ]] && [[ -n "$(git status --porcelain)" ]]; then
  if [[ -z "$MENSAJE" ]]; then
    err "hay cambios sin commitear: pasa -m \"mensaje\" o commitea tú primero"
    git status --short
    exit 1
  fi
  git add -A
  git commit -q -m "$MENSAJE"
  ok "commit en main: $(git log -1 --format='%h %s')"
else
  ok "sin cambios pendientes — publico lo ya commiteado"
fi

# ── 2. push a main (esto despliega Railway) ──────────────────────────────────
if git log --oneline origin/main..main | grep -q .; then
  if ! git push origin main 2>&1 | tail -3; then
    err "push a main RECHAZADO — alguien empujó. NO fuerzo: haz 'git pull --rebase' y reintenta"
    exit 1
  fi
  ok "push a main (Railway despliega desde aquí)"
else
  ok "main ya estaba al día en el remoto"
fi

# ── 3. merge main → desarrollo y push ─────────────────────────────────────────
DESC_ANTES=$(git ls-remote origin refs/heads/desarrollo | cut -f1)
PENDIENTES=$(git log --oneline "$DESC_ANTES"..origin/main | wc -l | tr -d ' ')

if [[ "$PENDIENTES" -gt 0 ]]; then
  git checkout -q -B desarrollo "$DESC_ANTES"
  if ! git merge --no-ff origin/main -m "merge: sincroniza desarrollo con main

Trae los fixes de main a desarrollo conservando las guardas G4-G13.
main NO se toca: las guardas siguen siendo exclusivas de desarrollo." 2>&1 | tail -3; then
    err "CONFLICTO al mergear main→desarrollo"
    git merge --abort 2>/dev/null || true
    git checkout -q main
    err "nada se publicó en desarrollo. Resuelve el conflicto a mano."
    exit 1
  fi
  if [[ -n "$(git diff --name-only --diff-filter=U)" ]]; then
    err "conflictos sin resolver:"
    git diff --name-only --diff-filter=U
    git merge --abort 2>/dev/null || true
    git checkout -q main
    exit 1
  fi
  ok "merge main→desarrollo limpio ($PENDIENTES commit/s)"

  if ! git push origin desarrollo 2>&1 | tail -3; then
    err "push a desarrollo RECHAZADO — alguien empujó. NO fuerzo."
    git checkout -q main
    exit 1
  fi
  ok "push a desarrollo"
else
  ok "desarrollo ya estaba al día"
fi

git checkout -q main

# ── 4. verificaciones finales ─────────────────────────────────────────────────
git fetch -q origin
R_MAIN=$(git ls-remote origin refs/heads/main | cut -f1)
R_DESC=$(git ls-remote origin refs/heads/desarrollo | cut -f1)

FALTAN=$(git log --oneline "$R_DESC".."$R_MAIN" | wc -l | tr -d ' ')
if [[ "$FALTAN" != "0" ]]; then
  err "desarrollo NO quedó al día ($FALTAN commits de main pendientes)"
  exit 1
fi
ok "desarrollo contiene TODOS los commits de main"

if git cat-file -e "$R_MAIN:app/guards.py" 2>/dev/null; then
  avisa "main contiene app/guards.py — debería ser exclusivo de desarrollo"
else
  ok "main sin las guardas (como debe ser)"
fi

echo "──────────────────────────────────────────────────────────────"
ok "publicado: main=${R_MAIN:0:8}  desarrollo=${R_DESC:0:8}"

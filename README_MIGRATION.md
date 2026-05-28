# Daily Call Pipeline — Migração

Pasta self-contained com toda a rotina: fetch HubSpot + transcrição Whisper para 4 agentes (Rúben, David, Sandra, Emanuel), accionada diariamente via launchd.

## Estrutura

```
daily_call_pipeline/
├── .env                                  # HUBSPOT_TOKEN, OPENAI_API_KEY
├── agents_config.py                      # paths relativos
├── daily_fetch.py                        # script principal
├── com.andre.dailycalls.plist.template   # plist com placeholders
├── install.sh                            # bootstrap macOS
├── requirements.txt
├── logs/                                 # vazia na 1ª execução
├── agents/
│   ├── Ruben_Call_Report/   (data/, reports/, build scripts)
│   ├── David_Call_Report/
│   ├── Sandra_Call_Report/
│   └── Emanuel_Call_Report/
└── DailyCalls.app/                       # wrapper FDA (será copiado p/ ~/Applications/)
```

## Pré-requisitos (máquina destino: macOS Apple Silicon)

```bash
# Homebrew (se ainda não instalado)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

brew install python@3.11 ffmpeg
```

## Instalar

```bash
# 1. Descomprimir o pacote para ~/Documents/Claude/
cd ~/Documents/Claude
tar -xzf ~/Downloads/daily_call_pipeline_YYYYMMDD.tar.gz

# 2. Confirmar .env (chaves)
cat ~/Documents/Claude/daily_call_pipeline/.env
# Deve mostrar HUBSPOT_TOKEN=... e OPENAI_API_KEY=...

# 3. Correr o instalador
cd ~/Documents/Claude/daily_call_pipeline
bash install.sh
```

## Passo manual obrigatório — Full Disk Access

O macOS bloqueia o launchd de aceder a `~/Documents` se o `.app` não estiver autorizado.

1. Abrir **System Settings → Privacy & Security → Full Disk Access**.
2. Clicar em **"+"**.
3. Navegar até `~/Applications/DailyCalls.app` e adicionar.
4. Confirmar que o toggle fica **ON**.

Sem este passo o launchd falha com `Operation not permitted`.

## Testar

```bash
# Forçar execução imediata
launchctl start com.andre.dailycalls

# Acompanhar logs
tail -f ~/Documents/Claude/daily_call_pipeline/logs/launchd.err
tail -f ~/Documents/Claude/daily_call_pipeline/logs/$(date +%Y-%m-%d).log

# Verificar carregamento
launchctl list | grep dailycalls
```

Resultado esperado:
- `launchd.err` vazio ou só com warnings benignos.
- `logs/<data>.log` mostra `=== Daily call pipeline ===`.
- Aparecem ficheiros em `agents/<agente>/data/<ddMMM>/`.

## Mudar horário

Editar `~/Library/LaunchAgents/com.andre.dailycalls.plist`, mudar `Hour`/`Minute`, depois:

```bash
launchctl unload ~/Library/LaunchAgents/com.andre.dailycalls.plist
launchctl load ~/Library/LaunchAgents/com.andre.dailycalls.plist
```

## Correr manualmente (debug)

```bash
cd ~/Documents/Claude/daily_call_pipeline
/opt/homebrew/bin/python3.11 daily_fetch.py                       # ontem, todos os agentes
/opt/homebrew/bin/python3.11 daily_fetch.py --date 2026-05-11
/opt/homebrew/bin/python3.11 daily_fetch.py --agent ruben
```

## Claude Code na máquina destino

Para que a Claude Code também corra lá (análise + PDFs semanais):

```bash
# Instalar Claude Code CLI
npm install -g @anthropic-ai/claude-code

# (opcional) Copiar memória e CLAUDE.md
cp -R ~/.claude /destination_machine/~/.claude  # via scp/rsync
```

Depois lançar `claude` dentro de `~/Documents/Claude/daily_call_pipeline/` e os scripts de análise/PDF (`build_weekly_pdf.py`, `consolidate_week.py` dentro de cada `agents/<agente>/`) ficam disponíveis.

## Troubleshooting

| Sintoma | Causa | Fix |
|---|---|---|
| `Operation not permitted` no `launchd.err` | FDA não dado ao `.app` | Adicionar `~/Applications/DailyCalls.app` ao FDA |
| `HUBSPOT_TOKEN not set` | `.env` em falta ou no sítio errado | `.env` tem de estar em `daily_call_pipeline/.env` |
| `python3.11: command not found` | Python 3.11 não instalado | `brew install python@3.11` |
| `ffprobe failed` | ffmpeg não instalado | `brew install ffmpeg` |
| Calls vazias | owner_id errado ou sem chamadas no dia | Confirmar `agents_config.py` IDs |

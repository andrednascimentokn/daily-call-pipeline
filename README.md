# Daily Call Pipeline

Todos os dias vai buscar as chamadas do dia anterior ao HubSpot, transcreve-as
(Whisper), analisa-as (DeepSeek) e envia por email um PDF de coaching por
comercial. À sexta envia um resumo semanal à gestão.

Comerciais e destinatários: `agents_config.py`. Regras de análise por negócio
(Real Estate, NOVA Digital, Funeral em Vida): `deepseek_analysis.py`. O
`CLAUDE.md` descreve o formato das análises e as regras de coaching.

## Operação

| | |
|---|---|
| Servidor | VPS `root@84.247.143.175`, código em `/opt/daily-calls` (clone git deste repositório) |
| Cron | `0 5 * * *` `run_pipeline.py` (diário) · `0 8 * * 5` `run_weekly.py` (sexta). O VPS está em Lisboa+1h |
| Logs | `/var/log/daily-calls/cron.log`; cópia de cada dia em GCS |
| Segredos | `/etc/daily-calls/.env` + `/etc/daily-calls/gcs-sa.json` (ver `.env.example`) |
| Armazenamento | GCS `gs://ai-call-reports` (dados, análises, contexto de cada comercial, relatórios) |

Deploy: `git push` aqui e, no VPS, `cd /opt/daily-calls && git pull`.

Correr à mão:

```
cd /opt/daily-calls && nohup venv/bin/python run_pipeline.py >> /var/log/daily-calls/cron.log 2>&1 &
venv/bin/python run_weekly.py --dry-run
```

Instalação num VPS novo: `install_vps.sh`.

### Dependências de contas pessoais

- **Os emails saem da conta `andre.nascimento@karma-network.com`** (`GMAIL_USER` +
  app password em `/etc/daily-calls/.env`). Se a conta for desativada ou a app
  password mudar, os relatórios deixam de sair. Idealmente, passar para uma conta de serviço/partilhada.
- `andre.nascimento@...` está em `_CC_KARMA` (`agents_config.py`), em `GMAIL_TO` e
  em `PIPELINE_FAILURE_EMAILS`. Atualizar quando mudar o responsável.
- O clone no VPS usa um token GitHub pessoal na URL do remote. Idealmente, trocar
  por uma deploy key do repositório na Karma-Data.

Também usam este bucket e estas transcrições: `karma-call-analytics` (KCA) e o
`sales-trainer`.

`README_MIGRATION.md` e `legacy/` são da versão antiga em macOS (launchd), já não usada.

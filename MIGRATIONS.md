# Migrations (Alembic)

O schema do banco agora é **versionado**. Nada de `create_all` cego: as mudanças
de tabela são descritas em migrações e aplicadas de forma controlada — assim o
Postgres em produção evolui **sem recriar tabelas nem perder dados**.

## No deploy (automático, nada manual)

O app roda `run_migrations()` ao subir (no lifespan), com lógica segura:

- **Banco novo** (sem tabelas) → `upgrade head` cria tudo.
- **Banco que já existia antes do Alembic** (tem `users`, não tem `alembic_version`)
  → `stamp head`: apenas **carimba** como atual, sem recriar nada.
- **Banco já versionado** → `upgrade head` aplica as migrações pendentes.

Ou seja: o primeiro deploy com Alembic no seu Postgres atual **não toca nos seus dados** —
só registra que ele está na versão inicial. A partir daí, toda migração nova entra sozinha.

## Quando mudar o schema (ex.: nova tabela de catálogo, custo por SKU)

1. Edite os modelos em `app/models.py`.
2. Gere a migração comparando os modelos com o banco:
   ```bash
   alembic revision --autogenerate -m "descricao curta da mudanca"
   ```
3. **Revise** o arquivo gerado em `alembic/versions/` (autogenerate erra em
   renomeações e tipos sutis; confira o `upgrade()`/`downgrade()`).
4. Commit + push. No próximo deploy, `upgrade head` aplica automaticamente.

## Comandos úteis

```bash
alembic current        # em que versão o banco está
alembic history        # linha do tempo das migrações
alembic upgrade head   # aplica tudo que falta (o deploy já faz isso)
alembic downgrade -1   # volta uma migração (use com cuidado)
alembic stamp head     # marca como atual sem rodar (casos de recuperação)
```

> A URL do banco **não** fica no `alembic.ini` — o `alembic/env.py` lê de
> `settings.database_url` (a mesma do app), já tratando `postgres://` → `postgresql://`.

# Auto-deploy через GitHub Actions

Workflow: [`.github/workflows/deploy.yml`](workflows/deploy.yml)

## Что делает

При push в `main` (или ручном запуске через UI):
1. GitHub Actions runner подключается по SSH к VPS Hetzner
2. На VPS: `git pull` → `mode=real` → `docker compose down/up --build`
3. Ждёт healthcheck (до 90 сек)
4. Проверяет что бот аутентифицировался в PO (до 60 сек)

Игнорирует push'ы которые меняют **только** доки (`*.md`) или CI-файлы — деплой не триггерится впустую.

## Секреты репозитория

Settings → Secrets and variables → Actions → **New repository secret**:

| Имя | Значение |
|---|---|
| `VPS_HOST` | `37.27.13.173` |
| `VPS_USER` | `root` |
| `VPS_PORT` | `22` |
| `VPS_SSH_KEY` | приватный ключ `~/.ssh/github_actions_deploy` целиком (с `-----BEGIN/END-----`) |

Публичный ключ (`~/.ssh/github_actions_deploy.pub`) уже добавлен в `/root/.ssh/authorized_keys` на VPS.

## Ручной запуск

Repo → Actions → "Deploy to VPS" → **Run workflow** → Run.

Полезно когда:
- Хочешь повторно задеплоить без коммита
- Тестируешь workflow после правок секретов

## История деплоев

Repo → Actions — список всех запусков. Клик на запуск → видны логи каждого шага. Зелёная галка = успех, красный крестик = упало (с подробной ошибкой).

## Откат на старый коммит

Самый простой способ:
```bash
# Локально
git revert <bad_commit>     # создаёт новый коммит-обратку
git push origin main         # триггерит автодеплой
```

Или: вручную через UI — найти старый коммит, нажать "Revert" в GitHub.

## Безопасность

- Приватный ключ `~/.ssh/github_actions_deploy` хранится **только** в GitHub Secrets (зашифрован), в логах маскируется как `***`
- Используется отдельный ключ, не основной — если скомпрометирован, отзывается удалением одной строки из `/root/.ssh/authorized_keys`
- VPS принимает SSH только по ключам (не паролю), порт 22 стандартный

## Откатить ключ если понадобится

```bash
# Удалить публичный ключ из VPS
ssh root@37.27.13.173 'sed -i "/github-actions@po-bot/d" ~/.ssh/authorized_keys'

# Удалить локальные файлы
rm ~/.ssh/github_actions_deploy ~/.ssh/github_actions_deploy.pub

# Удалить секрет VPS_SSH_KEY в GitHub UI
```

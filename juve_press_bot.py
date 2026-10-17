            f'{receipt.mode} #{receipt.message_id or "?"} | stato salvato'
        )
        time.sleep(0.8)

    pending = [article_from_journal(entry) for entry in journal.entries]
    pending.sort(key=lambda item: (item.published, item.source, item.title))
    for article in pending:
        if not is_collection_candidate(article.published, requested_dates):
            journal.remove(article.notification_key)
            continue
        try_delivery(article)

    def publish_discovered(article: Article) -> None:
        if article.notification_key in seen:
            return
        journal.add(article)
        try_delivery(article)

    collect_articles(session, requested_dates, on_article=publish_discovered)
    checkpoint_state_to_git()
    return sent_count


def run_worker(
    duration_seconds: float = DEFAULT_WORKER_DURATION_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    *,
    dry_run: bool = False,
    include_yesterday: bool = False,
    preview_messages: bool = False,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    if duration_seconds <= 0:
        raise ValueError('La durata del worker deve essere maggiore di zero.')
    if poll_interval_seconds <= 0:
        raise ValueError("L'intervallo del worker deve essere maggiore di zero.")
    monotonic = clock or time.monotonic
    sleeper = sleep or time.sleep
    deadline = monotonic() + duration_seconds
    cycle = 0
    touch_worker_heartbeat()
    print(f'[WORKER] attivo {duration_seconds / 60:.0f} min | pausa {poll_interval_seconds:.0f}s dopo ogni ciclo')
    while monotonic() < deadline:
        cycle += 1
        touch_worker_heartbeat()
        cycle_started = monotonic()
        sent_count = 0
        outcome = 'ok'
        try:
            sent_count = run(
                dry_run=dry_run,
                include_yesterday=include_yesterday,
                preview_messages=preview_messages,
            )
        except CollectionError as error:
            outcome = f'errore: {compact_log_text(error, 70)}'
            checkpoint_state_to_git()
        touch_worker_heartbeat()
        elapsed = monotonic() - cycle_started
        remaining = deadline - monotonic()
        if remaining <= 0:
            print(f'[CICLO {cycle}] nuove={sent_count} | {outcome} | {elapsed:.1f}s')
            break
        wait_seconds = min(poll_interval_seconds, remaining)
        print(f'[CICLO {cycle}] nuove={sent_count} | {outcome} | {elapsed:.1f}s | pausa={wait_seconds:.0f}s')
        sleeper(wait_seconds)
        touch_worker_heartbeat()
    print(f'\n[WORKER] fine | cicli={cycle} | arresto pulito')


def main() -> None:
    parser = argparse.ArgumentParser(description='Invia su Telegram le notizie Juventus pubblicate oggi.')
    parser.add_argument('--dry-run', action='store_true', help='Recupera e mostra le notizie senza usare Telegram.')
    parser.add_argument('--include-yesterday', action='store_true', help='Compatibilità: le notizie di ieri sono ora controllate automaticamente.')
    parser.add_argument('--preview-messages', action='store_true', help='Con --dry-run mostra il testo HTML esatto che verrebbe inviato a Telegram.')
    parser.add_argument('--worker', action='store_true', help='Ripete i controlli fino alla durata configurata.')
    parser.add_argument('--duration-seconds', type=float, default=DEFAULT_WORKER_DURATION_SECONDS, help='Durata totale del worker (predefinita: 3300 secondi).')
    parser.add_argument('--interval-seconds', type=float, default=DEFAULT_POLL_INTERVAL_SECONDS, help='Pausa dopo ogni ciclo (predefinita: 15 secondi).')
    args = parser.parse_args()
    if args.preview_messages and not args.dry_run:
        parser.error('--preview-messages richiede --dry-run')
    if args.worker:
        run_worker(
            duration_seconds=args.duration_seconds,
            poll_interval_seconds=args.interval_seconds,
            dry_run=args.dry_run,
            include_yesterday=args.include_yesterday,
            preview_messages=args.preview_messages,
        )
    else:
        run(
            dry_run=args.dry_run,
            include_yesterday=args.include_yesterday,
            preview_messages=args.preview_messages,
        )


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Errore: {error}', file=sys.stderr)
        sys.exit(1)

# Status

Branch `feature/progression-core`. Usa só a biblioteca padrão do Python. O estado do coordenador pode ser salvo em SQLite; sem banco, tudo fica em memória. Ainda não há conexão com a corretora.

## Concluído

1. **Progressão:** `pocketautonomo/progression.py`
   - `PROGRESSION_CENTS = (500, 1000, 2000, 4000)` e `stake_for_attempt(1..4)`.
2. **Ciclo:** `pocketautonomo/cycle.py`, com testes em `tests/test_cycle.py`
   - Um ciclo por ativo e direção (CALL/PUT).
   - LOSS avança uma tentativa; a 4ª perda encerra sem 5ª entrada.
   - WIN, DRAW, CANCELLED e REJECTED encerram o ciclo na tentativa em que chegam, sem gale. `outcome` guarda o resultado que encerrou.
   - UNKNOWN bloqueia até chegar o resultado definitivo da mesma tentativa.
   - Resultado duplicado não avança; saldo insuficiente não libera a entrada.
3. **Coordenador:** `pocketautonomo/coordinator.py`, com testes em `tests/test_coordinator.py`
   - Um ciclo ativo por coordenador. Um novo ciclo só começa com o anterior encerrado e sem reserva pendente.
   - `reserve_next_entry(balance_cents)` devolve uma `Reservation` com `reservation_id` único (uuid4), `cycle_id`, tentativa e a entrada (ativo, direção, valor).
   - Enquanto a reserva estiver pendente, inclusive depois de UNKNOWN, nenhuma outra entrada é liberada.
   - Só o resultado com o id da reserva pendente altera o ciclo. Resultados de reservas antigas, de ciclos anteriores ou de outro coordenador não mexem no ciclo atual.
   - Todas as operações passam pelo mesmo `threading.Lock`.
4. **Resultados adicionais:** DRAW, CANCELLED e REJECTED no ciclo e no coordenador.
   - Encerram o ciclo e liberam a reserva pendente, com a mesma proteção contra duplicados e ids incorretos.
   - CANCELLED e REJECTED só devem ser registrados quando a corretora confirmar. Timeout ou erro de rede não comprovam nada: o executor registra UNKNOWN e a reserva continua pendente.
5. **Persistência:** `pocketautonomo/storage.py`, com testes em `tests/test_storage.py`
   - `SqliteStore(caminho)` guarda os ciclos (ativo, direção, resultado final) e as reservas (id, ciclo, tentativa, valor, resultado). Reserva sem resultado ou com UNKNOWN está pendente.
   - `Coordinator(store)` grava cada mudança antes de aplicá-la. A reserva já está gravada (com commit) quando é devolvida ao executor.
   - Se a gravação falhar, a operação gera `StorageError` e nada muda: nenhuma entrada é liberada, nenhum resultado é aplicado e nenhum ciclo começa.
   - Ao abrir com banco, o estado é restaurado e o coordenador começa pausado. Resultados continuam sendo aceitos, mas nenhum ciclo começa e nenhuma entrada é liberada até `resume()`. A reserva pendente restaurada nunca é devolvida de novo.
   - Depois de reiniciar, resultados já processados continuam sendo ignorados como duplicados e a numeração de ciclos continua de onde parou.
   - Um banco inconsistente (por exemplo, com duas reservas pendentes) é recusado com `StorageError`.
   - Os bancos de execução (`*.db`, `*.sqlite`, `*.sqlite3` e arquivos auxiliares) ficam fora do Git pelo `.gitignore`. Os testes usam bancos em pastas temporárias.

## Testes

    python3 -m unittest discover -s tests -v

Último resultado: 73 testes, todos passando, no Python 3.9.25. A suíte também passa no 3.10, 3.12, 3.13 e 3.14.

## Decisões em vigor

- Valores sempre em centavos inteiros; float é recusado.
- Resultados aceitos: WIN, LOSS, DRAW, CANCELLED, REJECTED e UNKNOWN. Qualquer outro texto, como `TIMEOUT`, gera `ValueError`.
- Repetir o mesmo resultado retorna `False` e não muda nada. UNKNOWN atrasado é ignorado. Um resultado definitivo diferente do já registrado gera `ValueError`.
- Id de reserva desconhecido gera `UnknownReservationError`. Iniciar um ciclo fora de hora gera `CoordinatorError`.
- Saldo insuficiente não encerra o ciclo; a entrada fica disponível de novo quando o saldo cobrir o valor.
- Com banco, o coordenador sempre abre pausado, inclusive na primeira execução com o banco vazio.
- A pausa bloqueia `start_cycle` e `reserve_next_entry`, mas não `record_result`.

## Em aberto para as próximas etapas

- Um ciclo restaurado sem reserva pendente continua da tentativa em que parou depois de `resume()`. Não há como abandoná-lo se o sinal já não valer.
- A reserva ainda não guarda o id da ordem na corretora, que vai ser necessário para conferir pendências depois de reiniciar.
- Uma reserva pendente só se resolve com resultado definitivo. Falta definir o que registrar quando a ordem comprovadamente nem chegou a ser enviada (falha local antes do envio).
- UNKNOWN não tem prazo: o bloqueio dura até chegar um resultado definitivo.
- Conexão com a corretora.

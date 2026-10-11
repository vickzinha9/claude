# Status

Branch `feature/progression-core`. O núcleo roda em memória e usa só a biblioteca padrão do Python. Ainda não há conexão com a corretora nem persistência: o estado se perde quando o processo termina.

## Concluído

1. **Progressão:** `pocketautonomo/progression.py`
   - `PROGRESSION_CENTS = (500, 1000, 2000, 4000)` e `stake_for_attempt(1..4)`.
2. **Ciclo:** `pocketautonomo/cycle.py`, com testes em `tests/test_cycle.py`
   - Um ciclo por ativo e direção (CALL/PUT).
   - WIN encerra; LOSS avança uma tentativa; a 4ª perda encerra sem 5ª entrada.
   - UNKNOWN bloqueia até chegar o resultado definitivo da mesma tentativa.
   - Resultado duplicado não avança; saldo insuficiente não libera a entrada.
3. **Coordenador:** `pocketautonomo/coordinator.py`, com testes em `tests/test_coordinator.py`
   - Um ciclo ativo por coordenador. Um novo ciclo só começa com o anterior encerrado e sem reserva pendente.
   - `reserve_next_entry(balance_cents)` devolve uma `Reservation` com `reservation_id` único (uuid4), `cycle_id`, tentativa e a entrada (ativo, direção, valor).
   - Enquanto a reserva estiver pendente, inclusive depois de UNKNOWN, nenhuma outra entrada é liberada.
   - Só o resultado com o id da reserva pendente altera o ciclo. Resultados de reservas antigas, de ciclos anteriores ou de outro coordenador não mexem no ciclo atual.
   - Todas as operações passam pelo mesmo `threading.Lock`.

## Testes

    python3 -m unittest discover -s tests -v

Último resultado: 50 testes, todos passando, no Python 3.9.25. A suíte também passa no 3.10, 3.12 e 3.14.

## Decisões em vigor

- Valores sempre em centavos inteiros; float é recusado.
- Repetir o mesmo resultado retorna `False` e não muda nada. UNKNOWN atrasado é ignorado. Um resultado definitivo diferente do já registrado gera `ValueError`.
- Id de reserva desconhecido gera `UnknownReservationError`. Iniciar um ciclo fora de hora gera `CoordinatorError`.
- Saldo insuficiente não encerra o ciclo; a entrada fica disponível de novo quando o saldo cobrir o valor.

## Em aberto para as próximas etapas

- Não há como cancelar uma reserva. Se a ordem não chegar a ser enviada, a reserva fica pendente e o coordenador para de liberar entradas.
- UNKNOWN não tem prazo: o bloqueio dura até chegar WIN ou LOSS.
- Empate (DRAW) não é aceito.
- Conexão com a corretora e persistência do estado.

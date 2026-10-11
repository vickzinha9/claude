# Status

Branch `feature/progression-core`. Usa só a biblioteca padrão do Python. O estado do coordenador pode ser salvo em SQLite, e só um coordenador por vez opera cada banco; sem banco, tudo fica em memória. Ainda não há conexão com a corretora.

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
6. **Recuperação após reinício:** `pocketautonomo/coordinator.py`, com testes em `tests/test_storage.py`
   - Um ciclo restaurado em aberto conta como decisão vencida: não libera nenhuma entrada nova, nem depois de `resume()`. Enquanto ele estiver aberto, `status().recovery_pending` fica `True` e nenhum ciclo novo começa.
   - A recuperação termina quando o ciclo antigo se encerra: pelo resultado definitivo da reserva pendente (aceito mesmo com o coordenador pausado) ou por `abandon_cycle()`. Um LOSS que ainda deixa outra tentativa mantém a recuperação bloqueada; a 4ª perda encerra o ciclo sem 5ª entrada.
   - `abandon_cycle()` encerra o ciclo aberto sem registrar resultado de aposta: o `outcome` do ciclo vira ABANDONED e nenhuma tentativa recebe resultado. O abandono é gravado antes de mudar o estado em memória e continua valendo depois de reiniciar.
   - O abandono é recusado com `CoordinatorError` enquanto houver reserva pendente, inclusive com UNKNOWN. Também vale fora da recuperação, para um ciclo aberto sem reserva pendente.
7. **Exclusividade entre processos:** `pocketautonomo/storage.py`, com testes em `tests/test_exclusivity.py`
   - `SqliteStore(caminho)` toma uma trava exclusiva do sistema operacional antes de tocar no banco, portanto antes de carregar o estado e de liberar qualquer reserva. A trava fica no arquivo `<banco>-lock`: `fcntl.flock` em Linux/macOS e `msvcrt.locking` no Windows.
   - O sistema operacional libera a trava quando o arquivo é fechado ou o processo termina, inclusive à força. O arquivo `-lock` pode continuar no disco: o que protege é a trava, não a existência do arquivo.
   - A segunda instância, no mesmo processo ou em outro, falha com `DatabaseInUseError` ("another coordinator is already using <banco>; close it before opening this database again ...") sem abrir o banco, portanto sem alterar o estado.
   - `connect_read_only(caminho)` abre uma conexão independente só de leitura, que funciona com o coordenador rodando; o SQLite recusa escrita por ela.
   - `Coordinator.close()` fecha o coordenador e o banco e libera a trava; também funciona com `with`. Fechar de novo não faz nada. Depois de fechado, qualquer operação gera `CoordinatorError` e o `SqliteStore` fechado gera `StorageError`.
   - Os testes usam dois processos reais: o segundo é recusado enquanto o primeiro está aberto, abre depois que o primeiro fecha e também depois que o primeiro é encerrado com `kill`. Nos testes de reinício, a instância anterior é fechada antes de abrir outra.

## Testes

    python3 -m unittest discover -s tests -v

Último resultado: 85 testes, todos passando, no Python 3.9.25. A suíte também passa no 3.10, 3.12, 3.13 e 3.14. Tudo foi executado em Linux; o caminho do Windows (`msvcrt.locking`) ainda não foi executado.

## Decisões em vigor

- Valores sempre em centavos inteiros; float é recusado.
- Resultados aceitos: WIN, LOSS, DRAW, CANCELLED, REJECTED e UNKNOWN. Qualquer outro texto, como `TIMEOUT`, gera `ValueError`.
- Repetir o mesmo resultado retorna `False` e não muda nada. UNKNOWN atrasado é ignorado. Um resultado definitivo diferente do já registrado gera `ValueError`.
- Id de reserva desconhecido gera `UnknownReservationError`. Iniciar um ciclo fora de hora gera `CoordinatorError`.
- Saldo insuficiente não encerra o ciclo; a entrada fica disponível de novo quando o saldo cobrir o valor.
- Com banco, o coordenador sempre abre pausado, inclusive na primeira execução com o banco vazio.
- A pausa bloqueia `start_cycle` e `reserve_next_entry`, mas não `record_result` nem `abandon_cycle`.
- ABANDONED não é resultado de aposta: só `abandon_cycle()` o produz e `record_result` o recusa com `ValueError`.
- Todo ciclo restaurado em aberto é tratado como vencido, por mais curto que tenha sido o reinício.
- O coordenador é dono do banco que recebe: `close()` fecha o banco e uma restauração recusada também o fecha, liberando a trava.
- A trava impede um segundo coordenador, mas não impede que outro programa escreva direto no SQLite. Para consultas, use `connect_read_only`.

## Em aberto para as próximas etapas

- Rodar a suíte no Windows para confirmar o caminho `msvcrt.locking`.
- Executor de ordens e conexão com a corretora.
- A reserva ainda não guarda o id da ordem na corretora, que vai ser necessário para conferir pendências depois de reiniciar.
- Uma reserva pendente só se resolve com resultado definitivo. Falta definir o que registrar quando a ordem comprovadamente nem chegou a ser enviada (falha local antes do envio).
- UNKNOWN não tem prazo: o bloqueio dura até chegar um resultado definitivo.

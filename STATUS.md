# Status

Branch `feature/progression-core`. Usa só a biblioteca padrão do Python. O estado do coordenador, inclusive o estado de envio de cada reserva, pode ser salvo em SQLite, e só um coordenador por vez opera cada banco; sem banco, tudo fica em memória. **Ainda não existe executor de ordens nem conexão com a corretora:** nenhuma ordem é enviada.

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
8. **Estados de envio (só persistência):** `pocketautonomo/submission.py` e `pocketautonomo/storage.py`, com testes em `tests/test_submission_storage.py`
   - Estados: RESERVED, SUBMITTING, ACCEPTED e UNVERIFIED. As reservas ganharam as colunas `submission_state` e `broker_order_id`.
   - **Migração:** a versão do esquema fica em `PRAGMA user_version`; os bancos anteriores estão na versão 0. Ao abrir, o banco é levado à versão 1 numa única transação, só adicionando colunas e um índice, sem reescrever dados.
     - Toda reserva que já existia vira UNVERIFIED, sem id de ordem: pode ter sido enviada, então nunca pode iniciar um envio.
     - Se qualquer passo falhar, nada fica gravado e a abertura gera `StorageError`.
     - Um banco com versão de esquema desconhecida é recusado sem alteração.
   - `save_reservation` grava a nova reserva como RESERVED, sem id de ordem. `load()` manteve o formato.
   - `load_submissions()` devolve `{reservation_id: (submission_state, broker_order_id)}` e recusa linhas inconsistentes com `StorageError`, como um estado desconhecido ou ACCEPTED sem id.
   - `save_submission_start(reservation_id)` muda RESERVED → SUBMITTING, só para reserva sem resultado; UNKNOWN conta como resultado. O commit acontece antes do retorno. Uma segunda chamada é recusada com `StorageError`, inclusive depois de reiniciar.
   - `save_acceptance(reservation_id, broker_order_id)` muda SUBMITTING ou UNVERIFIED → ACCEPTED, também depois do resultado definitivo. O vínculo serve para conferência e histórico: não altera resultado nem ciclo. O id da ordem é gravado antes de retornar `True`.
     - Repetir a mesma aceitação retorna `False` e não muda nada, inclusive depois de reiniciar.
     - São recusados com `StorageError`: id diferente do já gravado, id que já pertence a outra reserva e reserva RESERVED.
   - O id da ordem precisa ser texto não vazio. O código recusa outros valores com `ValueError`. No banco, um índice único impede o mesmo id em duas reservas e uma restrição recusa texto vazio, só com espaços ou que não seja texto.
   - Toda gravação abre a transação com `BEGIN IMMEDIATE`, então a verificação e a escrita acontecem na mesma transação. Qualquer erro, inclusive no commit, desfaz tudo.
   - Uma falha ao gravar a aceitação deixa a reserva em SUBMITTING, que nunca permite novo envio.
9. **Estados de envio no coordenador:** `pocketautonomo/coordinator.py`, com testes em `tests/test_coordinator_submission.py`
   - `reserve_next_entry` devolve a reserva em RESERVED. O coordenador guarda em memória o estado de envio e o id da ordem de todas as reservas. `status()` mostra `submission_state` e `broker_order_id` da reserva pendente.
   - `begin_submission(reservation_id)` muda RESERVED → SUBMITTING e devolve a reserva; o envio à corretora só pode acontecer depois.
     - O início é gravado com `save_submission_start` antes de alterar a memória.
     - Recusa com `CoordinatorError`: coordenador pausado ou fechado, recuperação pendente, envios parados por falha de gravação, reserva que não é a pendente, reserva restaurada e segunda chamada, inclusive simultânea. Id desconhecido gera `UnknownReservationError`.
   - `confirm_acceptance(reservation_id, broker_order_id)` muda SUBMITTING ou UNVERIFIED → ACCEPTED, gravando antes de alterar a memória. É aceito com o coordenador pausado e durante a recuperação.
     - Repetição idêntica retorna `False`. São recusados com `CoordinatorError`: id diferente do já confirmado, id de outra reserva e reserva RESERVED.
     - Aceitação não é resultado: a reserva continua pendente até `record_result`.
     - Aceitação tardia, depois do resultado definitivo, só grava o vínculo: não muda resultado, tentativa nem ciclo e não libera entrada nem envio.
   - `record_result` recusa com `CoordinatorError` qualquer resultado, inclusive UNKNOWN, para a reserva pendente ainda RESERVED: ela nunca foi enviada.
   - **Falha de gravação** do início ou da aceitação gera `StorageError`, mantém a reserva pendente e para novos envios. A causa aparece em `status().submission_failure`.
     - Enquanto isso, `reserve_next_entry` devolve `None` e `start_cycle` e `begin_submission` são recusados. `resume()` não remove o bloqueio; só fechar e reabrir, para conferir o estado gravado.
     - Resultados e confirmações de aceitação continuam aceitos, porque só registram informação.
   - **Restauração:** usa `load_submissions()` e mantém o estado e o vínculo de todas as reservas, inclusive as já resolvidas. O coordenador continua abrindo pausado, e uma reserva restaurada nunca inicia envio, nem depois de `resume()`.

## Testes

    python3 -m unittest discover -s tests -v

Último resultado: 115 testes, todos passando, no Python 3.9.25. A suíte também passa no 3.10, 3.12, 3.13 e 3.14, todos com SQLite 3.40.0. Tudo foi executado em Linux; o caminho do Windows (`msvcrt.locking`) ainda não foi executado.

## Decisões em vigor

- Valores sempre em centavos inteiros; float é recusado.
- Resultados aceitos: WIN, LOSS, DRAW, CANCELLED, REJECTED e UNKNOWN. Qualquer outro texto, como `TIMEOUT`, gera `ValueError`.
- Repetir o mesmo resultado retorna `False` e não muda nada. UNKNOWN atrasado é ignorado. Um resultado definitivo diferente do já registrado gera `ValueError`.
- Id de reserva desconhecido gera `UnknownReservationError`. Iniciar um ciclo fora de hora gera `CoordinatorError`.
- Saldo insuficiente não encerra o ciclo; a entrada fica disponível de novo quando o saldo cobrir o valor.
- Com banco, o coordenador sempre abre pausado, inclusive na primeira execução com o banco vazio.
- A pausa bloqueia `start_cycle`, `reserve_next_entry` e `begin_submission`, mas não `record_result`, `confirm_acceptance` nem `abandon_cycle`.
- ABANDONED não é resultado de aposta: só `abandon_cycle()` o produz e `record_result` o recusa com `ValueError`.
- Todo ciclo restaurado em aberto é tratado como vencido, por mais curto que tenha sido o reinício.
- O coordenador é dono do banco que recebe: `close()` fecha o banco e uma restauração recusada também o fecha, liberando a trava.
- A trava impede um segundo coordenador, mas não impede que outro programa escreva direto no SQLite. Para consultas, use `connect_read_only`.
- Na coluna `submission_state`, o valor padrão é UNVERIFIED. Uma inserção que não informe o estado, por exemplo de uma versão anterior do programa, gera uma reserva que nunca pode iniciar envio.
- Aceitação tardia, depois do resultado definitivo, é permitida para reserva SUBMITTING ou UNVERIFIED e serve só para conferência e histórico. O id da ordem continua único.
- Uma migração já publicada nunca é editada; mudanças de esquema entram como nova migração.
- Só o primeiro erro de gravação de envio fica em `submission_failure`; erros seguintes não o substituem.

## Em aberto para as próximas etapas

- **Executor de ordens: pendente.** Nada chama `begin_submission` ou `confirm_acceptance` fora dos testes.
- **Conexão com a Pocket Option: pendente.** Nenhuma ordem é enviada nem consultada.
- **Reserva RESERVED sem saída:** uma reserva pendente que nunca iniciou envio não recebe resultado, não pode ser abandonada e não inicia envio se tiver sido restaurada. Isso trava o coordenador até existir o encerramento NOT_SUBMITTED. Acontece quando:
  - o processo para entre a reserva e o início do envio;
  - o executor decide não enviar;
  - a gravação do início falha.
- Consulta de pendências pelo id da ordem e o que fazer com UNVERIFIED ou SUBMITTING sem id.
- UNKNOWN não tem prazo: o bloqueio dura até chegar um resultado definitivo.
- Rodar a suíte no Windows para confirmar o caminho `msvcrt.locking` (CI em Linux e Windows).

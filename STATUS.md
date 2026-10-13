# Status

Branch `feature/progression-core`. Usa só a biblioteca padrão do Python. O estado do coordenador, inclusive o estado de envio de cada reserva, pode ser salvo em SQLite, e só um coordenador por vez opera cada banco; sem banco, tudo fica em memória. O executor envia a ordem de uma reserva e consulta o resultado da ordem pendente, testado só com corretoras falsas. **Ainda não existe conexão com a corretora:** nenhuma ordem real é enviada nem consultada.

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
   - `SqliteStore(caminho)` guarda os ciclos (ativo, direção, resultado final) e as reservas (id, ciclo, tentativa, valor, resultado). Reserva sem resultado ou com UNKNOWN está pendente, exceto se encerrada como NOT_SUBMITTED (item 10).
   - `Coordinator(store)` grava cada mudança antes de aplicá-la. A reserva já está gravada (com commit) quando é devolvida ao executor.
   - Se a gravação falhar, a operação gera `StorageError` e nada muda: nenhuma entrada é liberada, nenhum resultado é aplicado e nenhum ciclo começa.
   - Ao abrir com banco, o estado é restaurado e o coordenador começa pausado. Resultados continuam sendo aceitos, mas nenhum ciclo começa e nenhuma entrada é liberada até `resume()`. A reserva pendente restaurada nunca é devolvida de novo.
   - Depois de reiniciar, resultados já processados continuam sendo ignorados como duplicados e a numeração de ciclos continua de onde parou.
   - Um banco inconsistente (por exemplo, com duas reservas pendentes) é recusado com `StorageError`.
   - Os bancos de execução (`*.db`, `*.sqlite`, `*.sqlite3` e arquivos auxiliares) ficam fora do Git pelo `.gitignore`. Os testes usam bancos em pastas temporárias.
6. **Recuperação após reinício:** `pocketautonomo/coordinator.py`, com testes em `tests/test_storage.py`
   - Um ciclo restaurado em aberto conta como decisão vencida: não libera nenhuma entrada nova, nem depois de `resume()`. Enquanto ele estiver aberto, `status().recovery_pending` fica `True` e nenhum ciclo novo começa.
   - A recuperação termina quando o ciclo antigo se encerra: pelo resultado definitivo da reserva pendente (aceito mesmo com o coordenador pausado), por `close_not_submitted()` (item 11) ou por `abandon_cycle()`. Um LOSS que ainda deixa outra tentativa mantém a recuperação bloqueada; a 4ª perda encerra o ciclo sem 5ª entrada.
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
   - Estados: RESERVED, SUBMITTING, ACCEPTED e UNVERIFIED; NOT_SUBMITTED veio depois (item 10). As reservas ganharam as colunas `submission_state` e `broker_order_id`.
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
10. **Encerramento NOT_SUBMITTED (só persistência):** `pocketautonomo/submission.py` e `pocketautonomo/storage.py`, com testes em `tests/test_submission_storage.py`
    - NOT_SUBMITTED entrou em `SUBMISSION_STATES`. Não é resultado de aposta e não está em `RESULTS`. A coluna `submission_state` já existia, sem restrição da lista de estados: não houve coluna nova nem migração nova, e o esquema continua na versão 1.
    - `save_not_submitted(reservation_id)` aceita só reserva RESERVED, sem resultado, sem id de ordem e com o ciclo aberto. RESERVED é o único estado que prova que o envio não começou, porque o início é gravado como SUBMITTING antes de qualquer envio.
      - Na mesma transação, a reserva vira NOT_SUBMITTED e o ciclo vira ABANDONED; o banco recusa se outra reserva do ciclo continuar pendente. O commit acontece antes de retornar `True`. Nenhum resultado é gravado e a tentativa não avança.
      - Repetir um encerramento consistente retorna `False` e não muda nada, inclusive depois de reabrir o banco. Um encerramento inconsistente (ciclo reaberto, resultado, id de ordem ou nova pendência no ciclo) gera `StorageError`.
      - São recusados com `StorageError`, sem nenhuma alteração: SUBMITTING, ACCEPTED, UNVERIFIED, qualquer resultado (inclusive UNKNOWN), id de ordem e ciclo já encerrado. Uma falha em qualquer das duas atualizações desfaz as duas.
    - Pendente agora é: resultado NULL ou UNKNOWN e estado diferente de NOT_SUBMITTED. A mesma condição vale em `save_result`, `save_abandonment` e `save_not_submitted`.
    - Para uma reserva NOT_SUBMITTED, `save_result`, `save_submission_start` e `save_acceptance` geram `StorageError`.
    - `load()` manteve o formato: a reserva encerrada aparece sem resultado e o ciclo como ABANDONED. `load_submissions()` mostra NOT_SUBMITTED.
    - O Coordinator usa essa gravação em `close_not_submitted` (item 11).
11. **Encerramento NOT_SUBMITTED no coordenador:** `pocketautonomo/coordinator.py`, com testes em `tests/test_coordinator_not_submitted.py`
    - `close_not_submitted(reservation_id)` roda sob o lock. Um novo encerramento só vale para a reserva pendente em RESERVED, sem resultado e sem id de ordem. Funciona com o coordenador pausado, em recuperação e para reserva restaurada.
    - Com banco, grava com `save_not_submitted` antes de alterar a memória. Depois do commit, a reserva vira NOT_SUBMITTED, a pendência sai e o ciclo vira ABANDONED. Nenhum resultado de aposta é registrado, a tentativa não avança e o ciclo não libera gale. Sem banco, o comportamento é o mesmo.
    - São recusados com `CoordinatorError`: SUBMITTING, ACCEPTED, UNVERIFIED, reserva com UNKNOWN e reserva que não é a pendente. Id desconhecido gera `UnknownReservationError`.
    - Com `submission_failure` ativo, um novo encerramento é recusado até fechar e reabrir o coordenador, para conferir o estado gravado. O encerramento não limpa esse bloqueio.
    - Se a gravação do encerramento falhar, a reserva e o ciclo ficam como estavam em memória, `submission_failure` registra a causa e nenhum envio novo é permitido, nem o dessa reserva.
    - Repetir um encerramento concluído retorna `False` e não muda nada, nem o ciclo atual, inclusive depois de reiniciar. Resultado, aceitação e início de envio para a reserva NOT_SUBMITTED são recusados com `CoordinatorError`.
    - **Restauração:** a reserva NOT_SUBMITTED não é pendente. Ela precisa estar sem resultado e sem id de ordem, na tentativa que o ciclo esperava, e ser a última do ciclo; o ciclo precisa estar ABANDONED. O encerramento do ciclo é reproduzido uma única vez, sem resultado. Qualquer divergência gera `StorageError`. Resultados e vínculos anteriores continuam valendo, e o coordenador continua abrindo pausado.
    - Com uma reserva pendente nunca enviada, `abandon_cycle()` continua recusado, mas a mensagem agora indica `close_not_submitted()`.
12. **Executor de ordens (só o envio inicial):** `pocketautonomo/broker.py` e `pocketautonomo/executor.py`, com testes em `tests/test_executor.py`
    - `broker.py` é o contrato mínimo com a corretora: `OrderRequest` (reservation_id, asset, direction, stake_cents), `SubmissionResponse` (aceitação com `broker_order_id` ou rejeição explícita, sem id) e `Broker.submit_order(request)`. Timeout, erro de rede ou resposta pouco clara devem gerar exceção, nunca uma rejeição.
    - `OrderExecutor(coordinator, broker).submit(reservation_id)` chama `begin_submission` antes da corretora, então SUBMITTING já está gravado quando a ordem sai. Se o início for recusado ou falhar, a corretora não é chamada e a reserva fica como estava, sem encerramento automático.
    - A ordem leva exatamente o ativo, a direção, o valor em centavos e o id da reserva devolvida pelo coordenador. Chamadas repetidas ou simultâneas para a mesma reserva geram no máximo um envio, porque `begin_submission` só aceita RESERVED uma vez.
    - Aceitação com id válido chama `confirm_acceptance`, e a aposta continua pendente até o resultado. Rejeição explícita registra REJECTED e encerra o ciclo.
    - Exceção da corretora (timeout, erro de rede ou outra) ou resposta inválida: a reserva continua pendente e o executor registra UNKNOWN quando consegue. Ele nunca reenvia, registra LOSS ou encerra como NOT_SUBMITTED. Se gravar UNKNOWN também falhar, a pendência continua e o relatório traz os dois erros.
    - Se uma resposta explícita chegar mas sua gravação falhar, o relatório traz a resposta, inclusive o id da ordem realmente recebido, e o erro, sem dizer que ela foi gravada. Nada mais é gravado; o coordenador mantém a pendência e os bloqueios que já existiam.
    - `submit` não gera exceção nesses casos: devolve um `SubmissionReport` com `outcome` NOT_STARTED, ACCEPTANCE_RECORDED, REJECTION_RECORDED ou UNCERTAIN, o pedido enviado, a resposta válida recebida, `broker_order_id`, `unknown_recorded` e os erros.
    - Os testes usam uma corretora falsa definida no próprio arquivo de testes, com ids `FAKE-...`; nenhuma ordem chega a uma corretora real. Erro de rede, resposta inválida e falha ao gravar a rejeição não têm teste na suíte: foram conferidos só por um script avulso, em `75d66ac`.
13. **Consulta do resultado:** `pocketautonomo/broker.py` e `pocketautonomo/executor.py`, com testes em `tests/test_reconciliation.py`
    - O contrato ganhou `OrderStatus` (`broker_order_id`, `state`), congelado, e `Broker.get_order(broker_order_id)`. O `get_order` padrão levanta `NotImplementedError` sem ser abstrato, então uma corretora que só envia ordens continua instanciável.
    - `OrderExecutor.reconcile_pending()` lê `status()` uma vez e consulta, com `get_order`, só a ordem vinculada à reserva pendente. Nunca procura ordem por valor, horário ou aproximação e nunca envia nada. Funciona com o coordenador pausado, inclusive depois de reiniciar; pausa e recuperação seguem as regras do coordenador.
    - Devolve um `ReconciliationReport` congelado (`outcome`, `reservation_id`, `broker_order_id`, `result`, `error`):
      - `NO_PENDING`: não há reserva pendente;
      - `NO_ORDER_ID`: a reserva pendente não tem id vinculado; nada é consultado, e `error` informa o estado de envio;
      - `PENDING`: sem resultado definitivo e sem alteração. `error` fica vazio para `OPEN` e `UNKNOWN` e descreve os outros casos: consulta que falhou, resposta que não é `OrderStatus`, id diferente ou estado desconhecido;
      - `RESULT_RECORDED`: `record_result` aplicou o resultado definitivo. `RESULT_ALREADY_RECORDED`: o mesmo resultado já estava registrado;
      - `RECORD_FAILED`: `record_result` falhou; `result` traz o resultado recebido e `error`, o motivo.
    - **Testes na suíte:** `tests/test_reconciliation.py`, com uma corretora falsa de ids `FAKE-...`:
      - `test_loss_advances_once`: LOSS confirmado é gravado e avança uma única tentativa, para 1000 centavos; a segunda consulta devolve `NO_PENDING`, sem nova consulta nem novo envio;
      - `test_win_after_restart_ends_cycle`: depois de fechar e reabrir o banco, ainda pausado, a consulta usa o id restaurado, grava WIN e encerra o ciclo no banco, sem reenvio;
      - `test_without_order_id_does_not_query`: reservas RESERVED e SUBMITTING sem id dão `NO_ORDER_ID`, sem chamar a corretora e sem alterar nada;
      - `test_unsettled_or_invalid_answer_keeps_pending`: OPEN, UNKNOWN, timeout, id diferente, resposta que não é `OrderStatus` e estado desconhecido dão `PENDING`, sem alteração nem envio, com o erro no relatório quando cabe;
      - `test_concurrent_reconciliation_records_once`: duas threads consultam a mesma ordem, e uma barreira dentro da consulta falsa garante que as duas capturem a mesma pendência antes de receber LOSS. Uma dá `RESULT_RECORDED` e a outra `RESULT_ALREADY_RECORDED`; a tentativa avança só para 2, o LOSS fica gravado e houve um único envio;
      - `test_failed_result_write_keeps_pending`: com o SQLite recusando a escrita, LOSS dá `RECORD_FAILED`, com o resultado e o erro no relatório, sem alterar o coordenador nem o banco. Sem a falha, a nova consulta grava o LOSS uma vez e libera a tentativa de 1000 centavos, sem reenvio;
      - `test_progression_stops_at_four_losses_or_win`: com um banco por cenário, quatro LOSS enviam 500, 1000, 2000 e 4000 centavos, com o mesmo ativo e a mesma direção, e encerram o ciclo sem quinta ordem; LOSS, LOSS e WIN enviam 500, 1000 e 2000 e encerram na vitória, sem entrada de 4000. Um cenário usa CALL e o outro PUT. Cada reserva é enviada uma vez, e os ids vinculados e os resultados ficam gravados.
    - **Só verificação avulsa, fora da suíte:**
      - em `918f7ca`, um script conferiu que `submit_order` é o único método abstrato, que o `get_order` padrão levanta `NotImplementedError` e que `OrderStatus` não aceita alteração;
      - em `89fda85`, um script rodou cada caminho de `reconcile_pending` uma vez, em memória. Continuam sem teste na suíte: `RECORD_FAILED` causado por um resultado conflitante já registrado e a consulta pelo `get_order` padrão (`PENDING` com `NotImplementedError`).

## Testes

    python3 -m unittest discover -s tests -v

Último resultado: 144 testes, todos passando, no Python 3.9.25. No 3.10, 3.12, 3.13 e 3.14, a última execução foi em `75d66ac`, com 137 testes, todos com SQLite 3.40.0; as mudanças seguintes só rodaram no 3.9.25. Tudo foi executado em Linux; o caminho do Windows (`msvcrt.locking`) ainda não foi executado.

## Decisões em vigor

- Valores sempre em centavos inteiros; float é recusado.
- Resultados aceitos: WIN, LOSS, DRAW, CANCELLED, REJECTED e UNKNOWN. Qualquer outro texto, como `TIMEOUT`, gera `ValueError`.
- Repetir o mesmo resultado retorna `False` e não muda nada. UNKNOWN atrasado é ignorado. Um resultado definitivo diferente do já registrado gera `ValueError`.
- Id de reserva desconhecido gera `UnknownReservationError`. Iniciar um ciclo fora de hora gera `CoordinatorError`.
- Saldo insuficiente não encerra o ciclo; a entrada fica disponível de novo quando o saldo cobrir o valor.
- Com banco, o coordenador sempre abre pausado, inclusive na primeira execução com o banco vazio.
- A pausa bloqueia `start_cycle`, `reserve_next_entry` e `begin_submission`, mas não `record_result`, `confirm_acceptance`, `close_not_submitted` nem `abandon_cycle`.
- ABANDONED não é resultado de aposta: só `abandon_cycle()` o produz e `record_result` o recusa com `ValueError`.
- Todo ciclo restaurado em aberto é tratado como vencido, por mais curto que tenha sido o reinício.
- O coordenador é dono do banco que recebe: `close()` fecha o banco e uma restauração recusada também o fecha, liberando a trava.
- A trava impede um segundo coordenador, mas não impede que outro programa escreva direto no SQLite. Para consultas, use `connect_read_only`.
- Na coluna `submission_state`, o valor padrão é UNVERIFIED. Uma inserção que não informe o estado, por exemplo de uma versão anterior do programa, gera uma reserva que nunca pode iniciar envio.
- Aceitação tardia, depois do resultado definitivo, é permitida para reserva SUBMITTING ou UNVERIFIED e serve só para conferência e histórico. O id da ordem continua único.
- Uma migração já publicada nunca é editada; mudanças de esquema entram como nova migração.
- Só o primeiro erro de gravação de envio fica em `submission_failure`; erros seguintes não o substituem.
- NOT_SUBMITTED não é resultado de aposta: só `close_not_submitted`, por meio de `save_not_submitted`, o grava, e a reserva encerrada nunca recebe resultado, início de envio nem aceitação.
- `close_not_submitted` não funciona com `submission_failure` ativo, e uma falha ao gravar o encerramento também para novos envios: nos dois casos, é preciso fechar e reabrir para conferir o estado gravado.
- Só RESERVED prova que o envio não começou. Timeout, erro de rede ou falta de id de ordem não provam nada: SUBMITTING, ACCEPTED, UNVERIFIED e UNKNOWN nunca viram NOT_SUBMITTED.
- No envio (`submit`), o executor registra UNKNOWN só quando a resposta da corretora é desconhecida (exceção ou resposta inválida). Quando a resposta é explícita mas não pode ser gravada, ele a devolve no relatório, sem registrar outro resultado.
- O executor nunca repete um envio nem chama `close_not_submitted`.
- Na consulta (`reconcile_pending`), os estados reconhecidos são `OPEN` (constante `ORDER_OPEN`, em `executor.py`), `UNKNOWN` e os de `cycle.DEFINITIVE_RESULTS`, com comparação exata. Uma resposta que não seja `OrderStatus`, que fale de outra ordem ou que tenha estado desconhecido nunca é usada, mesmo trazendo um resultado definitivo.
- UNKNOWN informado pela corretora na consulta não é registrado no coordenador: a pendência fica como estava.
- Com o coordenador fechado, `reconcile_pending` não devolve relatório: o `CoordinatorError` de `status()` sobe.
- Uma falha ao gravar o resultado consultado mantém a reserva pendente e não ativa `submission_failure`. Como a pendência impede nova entrada, nada é enviado até uma nova consulta gravar o resultado.

## Em aberto para as próximas etapas

- **Executor de ordens:** o envio e a consulta do resultado existem e têm testes, mas nada os chama fora dos testes. Faltam a corretora simulada completa, o armazenamento durável dos relatórios, horários, payout e a estratégia CALL/PUT.
- O id de uma aceitação que não pôde ser gravada só existe no `SubmissionReport`. Quem chama `submit` precisa guardá-lo ou registrá-lo em log até a recuperação; senão, a reserva fica SUBMITTING sem id.
- **Conexão com a Pocket Option: pendente.** Nenhuma ordem real é enviada nem consultada.
- Reservas UNVERIFIED ou SUBMITTING sem id ficam em `NO_ORDER_ID`, porque a consulta só usa o id vinculado. Falta decidir como recuperá-las.
- UNKNOWN não tem prazo: o bloqueio dura até chegar um resultado definitivo.
- Rodar a suíte no Windows para confirmar o caminho `msvcrt.locking` (CI em Linux e Windows).

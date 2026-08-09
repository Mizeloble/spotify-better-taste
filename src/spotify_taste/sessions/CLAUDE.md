# sessions/ 규약

- **`machine.py` 는 순수하다.** DB·HTTP·`time.time()` 금지. 시간은 오직 샘플의
  `sampled_at` 에서만 온다. 이 규칙이 깨지면 `sessions rebuild` 가 실시간
  결과를 재현할 수 없고, "수집과 해석의 분리"가 말뿐이 된다.
  (`test_machine_never_reads_the_wall_clock` 이 이걸 강제한다.)
- **판정 상수는 `MachineConfig` 에 둔다.** 코드에 숫자를 박으면 규칙을 바꿨을 때
  과거를 다시 계산할 수 없다.
- **의심스러우면 나눈다.** 되감기·반복 재생은 새 세션이다. 합치면 한 곡을
  5번 반복한 것이 한 번의 긴 재생으로 뭉개진다.
- **적립은 관측한 만큼만.** 앞으로 탐색한 구간, 오류 구간, 일시정지 구간은
  `ms_played` 에 넣지 않는다. 없는 청취를 지어내면 안 된다.
- **스킵은 버리지 않는다.** `min_emit_ms` 는 스크럽 아티팩트를 거르는 장치이지
  짧은 재생을 버리는 장치가 아니다. `track_change`/`rewind` 는 이를 우회한다.
- **완성도 지표 둘을 뭉개지 말 것.** `completion_ratio`(위치)와
  `listened_ratio`(시간)는 다른 질문에 답한다. 80% 로 스크럽 후 끝까지 두면
  전자는 1.0, 후자는 0.2 다. positive 라벨은 둘 다 요구한다.
- **실측과 추정을 섞지 않는다.** recently-played 에서 역산한 값은
  `inferred_completion_ratio` 로만 들어간다. `ms_played` 는 폴러 전용이다.

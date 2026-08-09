# db/ 규약

- **stdlib `sqlite3` 만 쓴다.** SQLAlchemy·Alembic·기타 ORM 을 들이지 않는다.
  근거는 `connection.py` 독스트링에 있다. 새 의존성을 추가하고 싶으면 그 근거를
  먼저 반박할 것.
- **SQL 은 전부 `queries.py` 의 이름 붙은 상수다.** 다른 모듈에 SQL 문자열을
  인라인으로 쓰지 않는다. 이유는 grep 가능성 — "이 컬럼을 누가 쓰지"에
  답할 수 있어야 스키마를 안전하게 고친다.
- **커넥션은 `connect()` 로만 연다.** `sqlite3.connect()` 직접 호출 금지.
  PRAGMA 가 빠진 커넥션은 외래키도 WAL 도 없이 조용히 동작한다.
- **쓰기는 `tx()` 안에서.** 한 틱이 통째로 성공하거나 통째로 실패해야 한다.
- **행을 DELETE 하지 않는다.** 중복·팬텀은 `superseded_by` / `suspect_phantom`
  플래그로 표시한다. 예외는 `db prune` 의 만료된 `raw_api_response` 뿐이다.
- 시간 컬럼은 예외 없이 **UTC epoch 밀리초 정수**다. 문자열 날짜를 넣지 않는다.
  (`local_date` 만 예외 — 생성 컬럼이고 KST 일자 그룹핑 전용이다.)
- 마이그레이션은 **전진 전용**이다. 기존 파일을 수정하지 말고 새 번호를 추가한다.
  이미 적용된 파일을 고치면 `db verify` 의 지문이 어긋난다.

-- Extended Streaming History 임포트가 가져오는 Spotify 자체 판정 필드.
--
-- reason_start: 재생이 어떻게 시작됐나 — clickrow(직접 클릭), trackdone
--   (이전 곡이 끝나 이어짐), fwdbtn, backbtn, appload, playbtn, remote …
--   'clickrow' 는 사람이 그 곡을 직접 골랐다는 뜻이라 분류에 값진 신호다
--   (규칙 반영은 이후 RULES_VERSION 에서).
-- skipped: Spotify 가 스스로 '스킵'으로 판정한 재생.
--
-- 둘 다 폴러/RP 경로에는 없는 정보라 NULL 이 기본이고, 그 NULL 자체가
-- "이 행은 export 출신이 아니다"를 말해 준다.
ALTER TABLE play_session ADD COLUMN reason_start TEXT;
ALTER TABLE play_session ADD COLUMN skipped INTEGER;

# 단계 7 배포 검증 결과

검증일: 2026-07-18 KST

## 산출물

- `dist/공시검색-MCP-0.3.3.mcpb`
- `dist/사용설명서.html`
- `dist/SHA256SUMS.txt`
- `app/assets/disclosure-detective.png`
- `app/assets/disclosure-detective.ico`

## 자동 검증

- 전체 회귀: `164 passed, 20 subtests passed`
- 고정 평가: `24/24`
- 컴파일: `python -m compileall -q app installer tests` 통과
- 공식 MCPB CLI `info`: 패키지 읽기 성공, 서명 없음 경고만 표시
- 공식 MCPB CLI `validate build/mcpb-root/manifest.json`: schema validation 통과, icon validation 통과
- MCPB 빌드: deterministic SHA-256 동일성 확인
- 패키지 allow-list: `_local_data`, `.env`, 테스트 fixture, 로그, git 메타데이터 제외 확인
- API 키: `user_config.dart_api_key.sensitive=true`, 패키지 본문에 평문 키 저장 없음
- 번들 서버: unpacked package에서 stdio `initialize` 응답 확인

## 수동 확인 필요

- 실제 Claude Desktop 확장 설치 UI에서 `.mcpb` 설치
- OpenDART API 키 입력 후 재시작
- 커넥터 또는 도구 목록 노출
- 속도우선 검색 결과의 DART 원문 링크 표시
- 심화 검색기능 안내와 미리보기 동작

## 보류 항목

- 패키지 서명: 현재 미서명 상태이며 공식 CLI가 `Not signed` 경고를 표시한다.
- 공개 배포 라이선스: 소유자 결정 필요.
- 조직 배포: Claude Team/Enterprise allowlist 또는 custom extension 업로드는 별도 관리자 권한 확인 필요.

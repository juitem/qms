# QSS (Quick Stack Symbolizer) – 설계 문서

> 폴더 안의 스택 덤프 로그들을 읽어서  
> **콜스택을 inline까지 풀어 다시 번호를 매기고**  
> `#N 주소 in 함수 파일:라인` 형태로 재구성하는 도구.

이 문서는 기존 `QMS (Quick Multi Symbolizer)`의 아이디어를 참고하되,  
**스택/프레임 재구성에 초점을 맞춘 새로운 도구(QSS)의 설계**를 정리한다.

---

## 1. 목표

### 1.1 입력 예시

하나의 로그 파일 안에 여러 개의 스택 덤프가 섞여 있고,  
각 스택은 `#0`부터 다시 시작하는 형태:

```text
#0 0xffff1111222 (/usr/lib/glibc-1.2.3.so+0x2341) (Buildid: 113123121abababbabbaba)
#1 0xeeeeaaa2222 main (/usr/apps/org.tizen.test/mainbin+0x3132) (Buildid: 123123123123123123)
#2 ...

... (중간에 다른 로그 라인들) ...

#0 0xaaaa0000bbb (/usr/lib/libother.so+0x10) (Buildid: deadbeef...)
#1 0xbbbb1111ccc (/usr/lib/libother.so+0x90) (Buildid: deadbeef...)
...
```

### 1.2 출력 목표

각 스택을 **심볼이 풀린 콜스택**으로 재구성:

- inline 함수까지 모두 펼쳐서,
- 프레임 번호 `#N`을 **연속적으로 다시 매기고**,
- 새로운 스택이 시작될 때마다 다시 `#0`부터 시작.

예:

```text
=== STACK 1 (foo.log: line 10) ===
#0 0xffff1111222 in inner_func /usr/src/glibc/inner.c:123
#1 0xffff1111222 in mid_wrapper /usr/src/glibc/wrap.c:45
#2 0xffff1111222 in top_level /usr/src/glibc/top.c:12
#3 0xeeeeaaa2222 in main /usr/apps/org.tizen.test/main.c:200

=== STACK 2 (foo.log: line 120) ===
#0 0xaaaa0000bbb in func_a /usr/src/other/a.c:10
#1 0xbbbb1111ccc in func_b /usr/src/other/b.c:90
...
```

---

## 2. 전체 파이프라인 개요

QSS 파이프라인은 다음 5단계로 나뉜다:

1. **파일/스택/주소 정보 추출** (Log Parser)  
2. **실행 ELF / 디버그 ELF 목록 및 존재 여부 정리** (ELF Resolver & Scanner)  
3. **주소 → 심볼 정보로 변환** (Symbolizer + Cache)  
4. **스택 재구성 및 프레임 번호 재부여** (Stack Rebuilder)  
5. **결과 파일/리포트 작성** (Writers)

---

## 3. 단계별 설계

### 3.1 단계 1: 파일에서 정보 추출 (Log Parser)

- input-dir 재귀 스캔  
- `#<idx> <addr> [func] (<elf>+<offset>) (Buildid: <id>)` 파싱  
- `#0` 등장 시 새로운 스택 생성  
- Stack/Frame 구조로 저장  

### 3.2 단계 2: 실행/디버그 ELF 목록 및 존재 여부

- rootfs 기반 실행파일 경로 매핑  
- GNU 모드에서는 debuglink → build-id → yocto 순으로 탐색  
- ELF/디버그 존재 여부를 기록하는 TSV 선택적 출력  

### 3.3 단계 3: 주소 → 심볼 변환 + 캐시

- 각 Frame → SymbolizationJob  
- target_elf 기준으로 job 그룹화  
- 캐시 키: (orig_elf, offset, build_id)  
- addr2line을 ELF 단위로 stdin 스트리밍  
- inline 체인을 파싱하여 저장  

### 3.4 단계 4: 스택 재구성 및 프레임 번호 재부여

- inline chain을 ExpandedFrame으로 확장  
- 정렬 기준: (orig_frame_idx, inline_depth)  
- 새 스택마다 #0부터 번호 재부여  
- fallback 정책: func_hint 사용 가능  

### 3.5 단계 5: 결과 파일 작성

#### stack-only 모드
- `foo.log.stack.txt` 생성  
- 스택만 깔끔하게 출력  

#### rewrite 모드
- 원본 로그의 스택 라인 아래에 symbolized 라인 삽입  
- append/replace 옵션 지원  

#### 리포트
- failed_frames.tsv  
- elf_list.tsv (optional)  

---

## 4. CLI 설계

```
qss.py   --input-dir ./stack_logs   --output-dir ./stack_sym   --rootfs /mnt/tizen-rootfs   --mode stack-only|rewrite   -llvm | -gnu   --debug-root /usr/lib/debug/.build-id   -c arm-linux-gnueabihf-   --cache-db symbol_cache.sqlite   --workers-symbol auto|N   --workers-rewrite auto|N   -d --benchmark --verbose
```

---

## 5. 내부 모듈 구성

- parser.py  
- resolver.py  
- symbolizer.py  
- stack_rebuilder.py  
- writer.py  
- cli.py / main.py  

---

## 6. 확장 아이디어

- 스택 유사도 기반 그룹핑  
- JSON 출력  
- 웹 기반 뷰어 연동 가능

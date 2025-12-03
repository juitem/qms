# QSS 연결 / 데이터 설계 (Con.md)

> 이 문서는 QSS(Quick Stack Symbolizer)의 **단계별 데이터 구조와 연결(Flow)** 을 정리한 설계 문서다.  
> 실제 구현 코드와는 분리해서, “각 단계가 무엇을 입력으로 받고 어떤 산출물을 내보내는지”를 중심으로 기록한다.

---

## 0. 전체 데이터 플로우 개요

QSS 파이프라인은 다음 5단계로 동작한다:

1. **Parser 단계** – 로그 파일에서 스택/프레임 메타데이터 추출  
2. **Resolver 단계** – 실행 ELF / 디버그 ELF 매핑 및 상태 조사  
3. **Symbolizer 단계** – 주소 → (함수, 파일, 라인) 심볼화 + 캐시  
4. **Rebuilder 단계** – inline 확장 + 프레임 번호 재부여  
5. **Writer 단계** – 최종 결과 파일/리포트 생성

각 단계는 다음과 같이 **입력(Input) → 처리(Process) → 산출물(Output)** 구조를 가진다.

---

## 1. Parser 단계 – 로그에서 스택/프레임 추출

### 1.1 역할

- `--input-dir` 아래의 텍스트 로그 파일들을 재귀적으로 탐색한다.
- 각 파일에서 `#<idx> <addr> [func_hint] (<orig_elf>+<offset>) (Buildid: <build_id>)` 패턴을 찾아 파싱한다.
- `#0`을 기준으로 “하나의 스택”을 식별하고, 스택 내부의 프레임들을 수집한다.

### 1.2 입력

- 디렉터리 구조:
  - `input_dir/**.log` (이름/확장자는 고정하지 않음, text 파일로 가정)
- 파일 단위의 원본 로그 내용 (그대로 문자열로 읽음)

### 1.3 출력(산출물)

#### A. 메모리 상의 Stack / Frame 구조

```python
Stack = {
  "file": "logs/foo.log",   # 상대 경로
  "stack_id": 0,            # 파일 내 스택 번호 (0,1,2,...)
  "start_line": 10,         # #0 이 등장한 라인 번호
  "frames": [Frame0, Frame1, ...]
}

Frame = {
  "orig_idx": 0,            # 원래 로그상의 #번호
  "addr": "0xffff1111222",
  "orig_elf": "/usr/lib/glibc-1.2.3.so",
  "offset": "0x2341",
  "build_id": "113123121abababbabbaba",
  "func_hint": None or "main",
  "raw_line": "#0 0xffff1111... (원본 한 줄)"
}
```

#### B. 평탄화된 Frame 리스트 (심볼화 준비용)

TSV 예시 컬럼:

```text
file    stack_id  orig_frame_idx  addr           orig_elf                      offset   build_id              func_hint
foo.log 0         0               0xffff1111222  /usr/lib/glibc-1.2.3.so       0x2341   113123121abababb...   -
foo.log 0         1               0xeeeeaaa2222  /usr/apps/org.tizen.test/...  0x3132   123123123123123123    main
```

(실제 구현에서는 메모리 객체만 쓰고, TSV는 옵션으로 출력 가능)

---

## 2. Resolver 단계 – ELF / 디버그 파일 매핑 및 상태 조사

### 2.1 역할

- Parser 단계에서 나온 `orig_elf`, `build_id` 정보를 기반으로:
  - 실제 rootfs 내의 실행 파일 경로를 찾고(`target_elf`),
  - GNU 모드에서는 디버그 파일 후보들도 탐색한다.
- 각 ELF/디버그 파일의 “존재 + 사용 가능 여부”를 상태 코드로 표현한다.
- 나중 Symbolizer 단계에서 사용할 **SymbolizationJob** 리스트를 생성한다.

### 2.2 입력

- Frame 리스트 (또는 Stack들에서 추출한 모든 Frame)
- `--rootfs` 경로
- `--debug-root` (GNU 모드 시)
- 모드 플래그: `-llvm` / `-gnu`

### 2.3 출력(산출물)

#### A. ELF 매핑 테이블

```python
ElfMap = {
  ("/usr/lib/glibc-1.2.3.so", "113123121abababbabbaba"): "/mnt/rootfs/usr/lib/glibc-1.2.3.so",
  ("/usr/apps/org.tizen.test/mainbin", "123123123123123123"): "/mnt/rootfs/usr/apps/org.tizen.test/mainbin.debug",
  ...
}
```

키는 `(orig_elf, build_id)` 조합, 값은 `target_elf` 경로.

#### B. ELF 상태 리포트 (elf_list.tsv)

**YES/NO가 아니라 상태 코드 사용**

- 컬럼 예시:

```text
orig_elf    target_elf                     elf_status           debug_status          build_id      note
/usr/lib/liba.so   /mnt/rootfs/usr/lib/liba.so   OK                    OK                   1131...      -
/usr/lib/libb.so   /mnt/rootfs/usr/lib/libb.so   NOT_FOUND             NOT_FOUND            2233...      elf not found
/usr/lib/libc.so   /mnt/rootfs/usr/lib/libc.so   OK                    MISMATCH_BUILD_ID    aabb...      debug file mismatch
/usr/lib/libd.so   /mnt/rootfs/usr/lib/libd.so   CORRUPTED             NOT_ELF              9911...      corrupted elf file
/usr/lib/libe.so   /mnt/rootfs/usr/lib/libe.so   OK                    INCOMPLETE           deadbeef     no .debug_info
/usr/lib/libf.so   /mnt/rootfs/usr/lib/libf.so   NO_READ_PERMISSION    UNKNOWN_ERROR        1234...      cannot open file
```

##### 상태 코드 정의

- `elf_status` / `debug_status` 공통 코드:

| 코드                   | 의미                                                         |
|------------------------|--------------------------------------------------------------|
| `OK`                   | 정상 ELF/디버그 파일, 읽기 가능                             |
| `NOT_FOUND`            | 파일 없음                                                   |
| `NO_READ_PERMISSION`   | 파일은 있으나 권한 부족                                     |
| `NOT_ELF`              | ELF 매직 불일치 – ELF 형식 아님                             |
| `CORRUPTED`            | ELF 구조 손상, readelf/addr2line 동작 불가                  |
| `INCOMPLETE`           | ELF는 읽히지만 디버그 섹션 누락                             |
| `UNSUPPORTED_COMPRESSED` | 지원하지 않는 압축/패킹 포맷                              |
| `MISMATCH_BUILD_ID`    | 디버그 파일의 build-id가 기대 값과 불일치                  |
| `READ_ERROR`           | I/O 에러 등으로 읽기 실패                                   |
| `UNKNOWN_ERROR`        | 기타 분류 불가 상황                                         |

#### C. SymbolizationJob 리스트

각 Frame에 대해 Symbolizer가 사용할 최종 job:

```python
SymbolizationJob = {
  "file": "logs/foo.log",
  "stack_id": 0,
  "orig_frame_idx": 0,
  "addr": "0xffff1111222",
  "orig_elf": "/usr/lib/glibc-1.2.3.so",
  "target_elf": "/mnt/rootfs/usr/lib/glibc-1.2.3.so",
  "offset": "0x2341",
  "build_id": "113123121abababbabbaba"
}
```

이 리스트는 `target_elf` 기준으로 그룹핑해서 Symbolizer로 전달된다.

---

## 3. Symbolizer 단계 – 주소 → 심볼 + 캐시

### 3.1 역할

- 각 SymbolizationJob에 대해:
  - 먼저 캐시에서 조회하고,
  - 캐시에 없으면 addr2line(또는 llvm-addr2line)을 실행해 inline 체인을 얻는다.
- ELF 단위로 프로세스를 띄워 stdin으로 offset 목록을 흘려보내고 결과를 읽는다.
- 결과를 메모리 SymbolMap과 캐시 DB에 저장한다.

### 3.2 입력

- SymbolizationJob 리스트
- ElfMap
- 캐시 설정 (`--cache-db` 유/무)
- addr2line 실행 설정 (모드, cross prefix, demangle 여부 등)

### 3.3 출력(산출물)

#### A. SymbolMap (메모리 객체)

키: `(file, stack_id, orig_frame_idx)`  
값: inline 체인 목록

```python
SymbolMap = {
  ("logs/foo.log", 0, 0): [
    { "func": "inner_func", "file": "/usr/src/glibc/inner.c", "line": 123 },
    { "func": "mid_wrapper", "file": "/usr/src/glibc/wrap.c", "line": 45 },
    { "func": "top_level", "file": "/usr/src/glibc/top.c", "line": 12 }
  ],
  ("logs/foo.log", 0, 1): [
    { "func": "main", "file": "/usr/apps/org.tizen.test/main.c", "line": 200 }
  ],
  ...
}
```

#### B. 캐시 DB (옵션)

SQLite 기반 캐시 스키마 예시:

```sql
CREATE TABLE IF NOT EXISTS symbols (
  orig_elf    TEXT,
  offset      TEXT,
  build_id    TEXT,
  inline_json TEXT,  -- inline 체인을 JSON 문자열로 저장
  PRIMARY KEY (orig_elf, offset, build_id)
);
```

- 새로 심볼화된 결과는 이 DB에 insert/update 된다.
- 이후 동일한 `(orig_elf, offset, build_id)`에 대해서는 addr2line 호출 없이 캐시만 조회.

#### C. 실패 프레임 리포트 (failed_frames.tsv)

심볼화가 실패한 프레임에 대한 정보:

```text
file    stack_id  orig_frame_idx  orig_elf                   offset   build_id   target_elf                      reason
foo.log 0         2               /usr/lib/libbroken.so      0x1234   1131...    /mnt/rootfs/usr/lib/libbroken.so  CORRUPTED
bar.log 1         0               /usr/lib/libnodebug.so     0x88     deadbeef   /mnt/rootfs/usr/lib/libnodebug.so INCOMPLETE
```

- `reason` 컬럼은 Resolver에서 사용한 status와 연동하거나, Symbolizer에서의 에러 상황을 추가로 표현 가능

---

## 4. Rebuilder 단계 – inline 확장 + 프레임 번호 재부여

### 4.1 역할

- 기존 Stack/Frame 구조와 SymbolMap을 결합해:
  - 각 원본 Frame을 하나 이상의 “ExpandedFrame”으로 확장하고,
  - 스택별로 정렬 후 새로운 연속 번호 `#0, #1, #2, ...`를 부여한다.

### 4.2 입력

- Parser 단계의 Stacks 리스트
- Symbolizer 단계의 SymbolMap

### 4.3 출력(산출물)

#### A. ExpandedFrame 리스트 (메모리 객체)

```python
ExpandedFrame = {
  "file": "logs/foo.log",
  "stack_id": 0,
  "orig_frame_idx": 0,
  "addr": "0xffff1111222",
  "inline_depth": 0,  # 0,1,2,...
  "func": "inner_func",
  "src_file": "/usr/src/glibc/inner.c",
  "src_line": 123
}
```

- 한 Frame에 inline이 3단계 있으면 inline_depth 0~2로 3개의 ExpandedFrame 생성.

#### B. RebuiltStack 구조

```python
RebuiltStack = {
  "file": "logs/foo.log",
  "stack_id": 0,
  "frames": [
    { "new_idx": 0, "addr": "0xffff...", "func": "inner_func", "file": "inner.c", "line": 123 },
    { "new_idx": 1, "addr": "0xffff...", "func": "mid_wrapper", "file": "wrap.c",  "line": 45  },
    { "new_idx": 2, "addr": "0xffff...", "func": "top_level",   "file": "top.c",   "line": 12  },
    { "new_idx": 3, "addr": "0xeeee...", "func": "main",        "file": "main.c",  "line": 200 },
  ]
}
```

- 스택 하나마다 `frames`는 이미 새 번호(`new_idx`)가 매겨진 상태.

#### C. Expanded Frames Dump (옵션: expanded_frames.tsv)

```text
file    stack_id  new_idx  orig_idx  inline_depth  addr           func         src_file                 src_line
foo.log 0         0        0         0             0xffff1111222  inner_func   /usr/src/glibc/inner.c  123
foo.log 0         1        0         1             0xffff1111222  mid_wrapper  /usr/src/glibc/wrap.c   45
foo.log 0         2        0         2             0xffff1111222  top_level    /usr/src/glibc/top.c    12
foo.log 0         3        1         0             0xeeeeaaa2222  main         /usr/apps/.../main.c    200
```

---

## 5. Writer 단계 – 최종 결과 파일 및 요약 생성

### 5.1 역할

- RebuiltStack 구조를 이용해 최종 결과를 파일로 쓴다.
- 모드에 따라 stack-only / rewrite / 요약 리포트를 생성한다.

### 5.2 입력

- RebuiltStack 리스트
- (rewrite 모드일 경우) 원본 로그 파일 내용

### 5.3 출력(산출물)

#### A. stack-only 결과 파일

- 파일 이름 예:
  - `logs/foo.log.stack.txt`

내용 예:

```text
=== STACK 0 (foo.log: line 10) ===
#0 0xffff1111222 in inner_func /usr/src/glibc/inner.c:123
#1 0xffff1111222 in mid_wrapper /usr/src/glibc/wrap.c:45
#2 0xffff1111222 in top_level /usr/src/glibc/top.c:12
#3 0xeeeeaaa2222 in main /usr/apps/org.tizen.test/main.c:200
```

#### B. rewrite 결과 파일

- 파일 이름 예:
  - `logs/foo.log.rewrite`

각 스택 프레임 라인 아래에 해석된 스택 정보를 추가:

```text
#0 0xffff1111222 (/usr/lib/glibc-1.2.3.so+0x2341) (Buildid: ...)
  -> #0 0xffff1111222 in inner_func /usr/src/glibc/inner.c:123
  -> #1 0xffff1111222 in mid_wrapper /usr/src/glibc/wrap.c:45
  -> #2 0xffff1111222 in top_level /usr/src/glibc/top.c:12

#1 0xeeeeaaa2222 main (/usr/apps/org.tizen.test/mainbin+0x3132) (Buildid: ...)
  -> #3 0xeeeeaaa2222 in main /usr/apps/org.tizen.test/main.c:200
```

- append / replace 모드에 따라 원본 라인을 유지하거나 교체 가능.

#### C. 요약 파일 (summary.json – 옵션)

예:

```json
{
  "total_input_files": 12,
  "total_stacks": 33,
  "total_frames": 201,
  "symbolized_frames": 186,
  "failed_frames": 15,
  "elf_status_counts": {
    "OK": 40,
    "NOT_FOUND": 3,
    "CORRUPTED": 1,
    "INCOMPLETE": 5
  }
}
```

---

## 6. 이 문서의 위치와 역할

- `Con.md`는 QSS의 **연결(Connection) / 데이터 흐름(Data Flow)** 에 대한 설계 문서다.
- 실제 CLI 사용법과 개략적인 소개는 `README2.md` (또는 `README.md`)에 기록한다.
- 향후 구현 단계에서:
  - 각 모듈(parser, resolver, symbolizer, rebuilder, writer)이
  - 이 문서의 “입력/출력 계약”을 지키는지 기준으로 테스트/검증을 진행한다.

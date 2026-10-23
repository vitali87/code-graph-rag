# Tomorrow's Shade — an animated short film made with raylib.
#
#   make            build (fetches and builds raylib 5.5 locally if needed)
#   make run        build and play the film
#   make video      render the film to tomorrows_shade.mp4 (needs ffmpeg)
#
# Point RAYLIB_SRC at an existing raylib checkout to skip the download.

RAYLIB_VERSION ?= 5.5
RAYLIB_SRC     ?= third_party/raylib/src
RAYLIB_LIB      = $(RAYLIB_SRC)/libraylib.a

CC      ?= cc
CFLAGS  ?= -O2 -std=c99 -Wall -Wextra -Wno-unused-parameter -Wno-missing-field-initializers
CFLAGS  += -I$(RAYLIB_SRC) -D_DEFAULT_SOURCE

UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
  LDLIBS = -framework OpenGL -framework Cocoa -framework IOKit -framework CoreAudio -framework CoreVideo -lm
else ifeq ($(OS),Windows_NT)
  LDLIBS = -lopengl32 -lgdi32 -lwinmm -lm
else
  LDLIBS = -lGL -lm -lpthread -ldl -lrt -lX11
endif

SRC  = src/main.c src/story.c src/character.c src/tree.c src/render.c src/audio.c src/particles.c src/grass.c src/mathx.c
OBJ  = $(SRC:.c=.o)
HDR  = $(wildcard src/*.h)
BIN  = tomorrows_shade

.PHONY: all run video clean distclean

all: $(BIN)

$(BIN): $(RAYLIB_LIB) $(OBJ)
	$(CC) $(OBJ) $(RAYLIB_LIB) $(LDLIBS) -o $@

src/%.o: src/%.c $(HDR) $(RAYLIB_LIB)
	$(CC) $(CFLAGS) -c $< -o $@

$(RAYLIB_LIB):
	@if [ ! -d "$(RAYLIB_SRC)" ]; then \
		mkdir -p third_party && \
		git clone --depth 1 --branch $(RAYLIB_VERSION) https://github.com/raysan5/raylib.git third_party/raylib; \
	fi
	$(MAKE) -C $(RAYLIB_SRC) PLATFORM=PLATFORM_DESKTOP GRAPHICS=GRAPHICS_API_OPENGL_33

run: $(BIN)
	./$(BIN)

video: $(BIN)
	./$(BIN) --record tomorrows_shade.mp4

clean:
	rm -f $(OBJ) $(BIN)

distclean: clean
	rm -rf third_party

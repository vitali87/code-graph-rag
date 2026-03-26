from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import run_updater


@pytest.fixture
def rust_web_project(temp_repo: Path) -> Path:
    """Create a Rust project with web examples."""
    project_path = temp_repo / "rust_web_test"
    project_path.mkdir()

    (project_path / "Cargo.toml").write_text(
        encoding="utf-8",
        data="""
[package]
name = "rust_web_test"
version = "0.1.0"
edition = "2021"

[dependencies]
tokio = { version = "1", features = ["full"] }
reqwest = { version = "0.11", features = ["json"] }
axum = "0.7"
serde = { version = "1.0", features = ["derive"] }
serde_json = "1.0"
""",
    )

    (project_path / "src").mkdir()
    (project_path / "src" / "lib.rs").write_text(
        encoding="utf-8", data="// Web test crate"
    )

    return project_path


def test_http_client_requests(
    rust_web_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test HTTP client request patterns."""
    test_file = rust_web_project / "http_client.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use reqwest;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Serialize, Deserialize)]
struct User {
    id: u32,
    name: String,
    email: String,
}

#[derive(Debug, Serialize, Deserialize)]
struct CreateUserRequest {
    name: String,
    email: String,
}

async fn get_user_by_id(client: &reqwest::Client, id: u32) -> Result<User, reqwest::Error> {
    let url = format!("https://api.example.com/users/{}", id);
    let response = client.get(&url).send().await?;
    let user: User = response.json().await?;
    Ok(user)
}

async fn create_user(client: &reqwest::Client, request: CreateUserRequest) -> Result<User, reqwest::Error> {
    let response = client
        .post("https://api.example.com/users")
        .json(&request)
        .send()
        .await?;

    let user: User = response.json().await?;
    Ok(user)
}

async fn update_user(client: &reqwest::Client, id: u32, request: CreateUserRequest) -> Result<User, reqwest::Error> {
    let url = format!("https://api.example.com/users/{}", id);
    let response = client
        .put(&url)
        .json(&request)
        .send()
        .await?;

    let user: User = response.json().await?;
    Ok(user)
}

async fn delete_user(client: &reqwest::Client, id: u32) -> Result<(), reqwest::Error> {
    let url = format!("https://api.example.com/users/{}", id);
    client.delete(&url).send().await?;
    Ok(())
}

async fn get_users_with_params(client: &reqwest::Client, page: u32, limit: u32) -> Result<Vec<User>, reqwest::Error> {
    let mut params = HashMap::new();
    params.insert("page", page.to_string());
    params.insert("limit", limit.to_string());

    let response = client
        .get("https://api.example.com/users")
        .query(&params)
        .send()
        .await?;

    let users: Vec<User> = response.json().await?;
    Ok(users)
}

async fn post_with_headers(client: &reqwest::Client) -> Result<String, reqwest::Error> {
    let response = client
        .post("https://api.example.com/protected")
        .header("Authorization", "Bearer token123")
        .header("Content-Type", "application/json")
        .body(r#"{"data": "sensitive"}"#)
        .send()
        .await?;

    let text = response.text().await?;
    Ok(text)
}

async fn download_file(client: &reqwest::Client, url: &str) -> Result<Vec<u8>, reqwest::Error> {
    let response = client.get(url).send().await?;
    let bytes = response.bytes().await?;
    Ok(bytes.to_vec())
}

async fn upload_file(client: &reqwest::Client, file_data: Vec<u8>) -> Result<String, reqwest::Error> {
    let part = reqwest::multipart::Part::bytes(file_data)
        .file_name("upload.txt")
        .mime_str("text/plain")?;

    let form = reqwest::multipart::Form::new()
        .part("file", part);

    let response = client
        .post("https://api.example.com/upload")
        .multipart(form)
        .send()
        .await?;

    let result = response.text().await?;
    Ok(result)
}

async fn handle_error_responses(client: &reqwest::Client) -> Result<User, Box<dyn std::error::Error>> {
    let response = client
        .get("https://api.example.com/users/999")
        .send()
        .await?;

    match response.status() {
        reqwest::StatusCode::OK => {
            let user: User = response.json().await?;
            Ok(user)
        },
        reqwest::StatusCode::NOT_FOUND => {
            Err("User not found".into())
        },
        reqwest::StatusCode::UNAUTHORIZED => {
            Err("Unauthorized access".into())
        },
        status => {
            Err(format!("Unexpected status: {}", status).into())
        }
    }
}

async fn client_with_timeout() -> reqwest::Client {
    reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .user_agent("MyApp/1.0")
        .build()
        .expect("Failed to create client")
}

async fn concurrent_requests(client: &reqwest::Client) -> Result<Vec<User>, Box<dyn std::error::Error>> {
    let user_ids = vec![1, 2, 3, 4, 5];

    let futures: Vec<_> = user_ids
        .into_iter()
        .map(|id| get_user_by_id(client, id))
        .collect();

    let results = futures::future::join_all(futures).await;

    let users: Result<Vec<User>, _> = results.into_iter().collect();
    Ok(users?)
}
""",
    )

    run_updater(rust_web_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    client_calls = [
        call
        for call in calls
        if "get_user_by_id" in str(call) or "create_user" in str(call)
    ]
    assert len(client_calls) > 0, "HTTP client functions should be detected"


def test_web_server_axum(
    rust_web_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Axum web server patterns."""
    test_file = rust_web_project / "axum_server.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::Json,
    routing::{get, post, put, delete},
    Router,
};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use tokio::net::TcpListener;

#[derive(Debug, Clone, Serialize, Deserialize)]
struct User {
    id: u32,
    name: String,
    email: String,
}

#[derive(Debug, Deserialize)]
struct CreateUserRequest {
    name: String,
    email: String,
}

#[derive(Debug, Deserialize)]
struct UpdateUserRequest {
    name: Option<String>,
    email: Option<String>,
}

#[derive(Debug, Deserialize)]
struct UserQuery {
    page: Option<u32>,
    limit: Option<u32>,
}

type UserDatabase = Arc<Mutex<HashMap<u32, User>>>;
type AppState = UserDatabase;

async fn get_users(
    Query(params): Query<UserQuery>,
    State(db): State<AppState>,
) -> Result<Json<Vec<User>>, StatusCode> {
    let db = db.lock().unwrap();
    let users: Vec<User> = db.values().cloned().collect();

    let page = params.page.unwrap_or(1);
    let limit = params.limit.unwrap_or(10);
    let start = ((page - 1) * limit) as usize;
    let end = (start + limit as usize).min(users.len());

    let paginated_users = users[start..end].to_vec();
    Ok(Json(paginated_users))
}

async fn get_user_by_id(
    Path(id): Path<u32>,
    State(db): State<AppState>,
) -> Result<Json<User>, StatusCode> {
    let db = db.lock().unwrap();
    match db.get(&id) {
        Some(user) => Ok(Json(user.clone())),
        None => Err(StatusCode::NOT_FOUND),
    }
}

async fn create_user(
    State(db): State<AppState>,
    Json(request): Json<CreateUserRequest>,
) -> Result<Json<User>, StatusCode> {
    let mut db = db.lock().unwrap();
    let id = db.len() as u32 + 1;

    let user = User {
        id,
        name: request.name,
        email: request.email,
    };

    db.insert(id, user.clone());
    Ok(Json(user))
}

async fn update_user(
    Path(id): Path<u32>,
    State(db): State<AppState>,
    Json(request): Json<UpdateUserRequest>,
) -> Result<Json<User>, StatusCode> {
    let mut db = db.lock().unwrap();

    match db.get_mut(&id) {
        Some(user) => {
            if let Some(name) = request.name {
                user.name = name;
            }
            if let Some(email) = request.email {
                user.email = email;
            }
            Ok(Json(user.clone()))
        },
        None => Err(StatusCode::NOT_FOUND),
    }
}

async fn delete_user(
    Path(id): Path<u32>,
    State(db): State<AppState>,
) -> Result<StatusCode, StatusCode> {
    let mut db = db.lock().unwrap();

    match db.remove(&id) {
        Some(_) => Ok(StatusCode::NO_CONTENT),
        None => Err(StatusCode::NOT_FOUND),
    }
}

async fn health_check() -> &'static str {
    "OK"
}

async fn user_stats(State(db): State<AppState>) -> Json<serde_json::Value> {
    let db = db.lock().unwrap();
    let total_users = db.len();
    let users: Vec<_> = db.values().cloned().collect();

    let mut domain_count = HashMap::new();
    for user in users {
        if let Some(domain) = user.email.split('@').nth(1) {
            *domain_count.entry(domain.to_string()).or_insert(0) += 1;
        }
    }

    Json(serde_json::json!({
        "total_users": total_users,
        "domains": domain_count
    }))
}

fn create_router() -> Router<AppState> {
    Router::new()
        .route("/health", get(health_check))
        .route("/users", get(get_users).post(create_user))
        .route("/users/:id", get(get_user_by_id).put(update_user).delete(delete_user))
        .route("/stats", get(user_stats))
}

async fn run_server() -> Result<(), Box<dyn std::error::Error>> {
    let database: UserDatabase = Arc::new(Mutex::new(HashMap::new()));

    // Add some sample data
    {
        let mut db = database.lock().unwrap();
        db.insert(1, User {
            id: 1,
            name: "Alice".to_string(),
            email: "alice@example.com".to_string(),
        });
        db.insert(2, User {
            id: 2,
            name: "Bob".to_string(),
            email: "bob@test.com".to_string(),
        });
    }

    let app = create_router().with_state(database);
    let listener = TcpListener::bind("0.0.0.0:3000").await?;

    println!("Server running on http://0.0.0.0:3000");
    axum::serve(listener, app).await?;

    Ok(())
}

mod middleware {
    use axum::{
        http::{Request, StatusCode},
        middleware::Next,
        response::Response,
    };

    pub async fn logging_middleware<B>(
        request: Request<B>,
        next: Next<B>,
    ) -> Result<Response, StatusCode> {
        let method = request.method().clone();
        let uri = request.uri().clone();

        println!("Request: {} {}", method, uri);

        let response = next.run(request).await;

        println!("Response status: {}", response.status());

        Ok(response)
    }

    pub async fn auth_middleware<B>(
        request: Request<B>,
        next: Next<B>,
    ) -> Result<Response, StatusCode> {
        let auth_header = request
            .headers()
            .get("Authorization")
            .and_then(|header| header.to_str().ok());

        match auth_header {
            Some(header) if header.starts_with("Bearer ") => {
                // Validate token (simplified)
                let token = &header[7..];
                if token == "valid_token" {
                    Ok(next.run(request).await)
                } else {
                    Err(StatusCode::UNAUTHORIZED)
                }
            },
            _ => Err(StatusCode::UNAUTHORIZED),
        }
    }
}
""",
    )

    run_updater(rust_web_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    server_calls = [
        call
        for call in calls
        if "create_router" in str(call) or "run_server" in str(call)
    ]
    assert len(server_calls) > 0, "Axum server functions should be detected"


def test_websockets_realtime(
    rust_web_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test WebSocket and real-time communication patterns."""
    test_file = rust_web_project / "websockets.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use tokio::sync::broadcast;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
enum WebSocketMessage {
    Join { room: String, user: String },
    Leave { room: String, user: String },
    Message { room: String, user: String, content: String },
    UserList { room: String, users: Vec<String> },
    Error { message: String },
}

#[derive(Debug, Clone)]
struct ChatRoom {
    name: String,
    users: Vec<String>,
    message_history: Vec<ChatMessage>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ChatMessage {
    user: String,
    content: String,
    timestamp: u64,
}

type Rooms = Arc<Mutex<HashMap<String, ChatRoom>>>;
type Broadcaster = broadcast::Sender<WebSocketMessage>;

struct ChatServer {
    rooms: Rooms,
    broadcaster: Broadcaster,
}

impl ChatServer {
    fn new() -> Self {
        let (broadcaster, _) = broadcast::channel(1000);

        ChatServer {
            rooms: Arc::new(Mutex::new(HashMap::new())),
            broadcaster,
        }
    }

    async fn handle_join(&self, room: String, user: String) -> Result<(), String> {
        let mut rooms = self.rooms.lock().map_err(|_| "Failed to lock rooms")?;

        let chat_room = rooms.entry(room.clone()).or_insert(ChatRoom {
            name: room.clone(),
            users: Vec::new(),
            message_history: Vec::new(),
        });

        if !chat_room.users.contains(&user) {
            chat_room.users.push(user.clone());
        }

        let user_list_msg = WebSocketMessage::UserList {
            room: room.clone(),
            users: chat_room.users.clone(),
        };

        let _ = self.broadcaster.send(user_list_msg);

        Ok(())
    }

    async fn handle_leave(&self, room: String, user: String) -> Result<(), String> {
        let mut rooms = self.rooms.lock().map_err(|_| "Failed to lock rooms")?;

        if let Some(chat_room) = rooms.get_mut(&room) {
            chat_room.users.retain(|u| u != &user);

            if chat_room.users.is_empty() {
                rooms.remove(&room);
            } else {
                let user_list_msg = WebSocketMessage::UserList {
                    room: room.clone(),
                    users: chat_room.users.clone(),
                };

                let _ = self.broadcaster.send(user_list_msg);
            }
        }

        Ok(())
    }

    async fn handle_message(&self, room: String, user: String, content: String) -> Result<(), String> {
        let mut rooms = self.rooms.lock().map_err(|_| "Failed to lock rooms")?;

        if let Some(chat_room) = rooms.get_mut(&room) {
            if !chat_room.users.contains(&user) {
                return Err("User not in room".to_string());
            }

            let chat_message = ChatMessage {
                user: user.clone(),
                content: content.clone(),
                timestamp: std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_secs(),
            };

            chat_room.message_history.push(chat_message);

            // Keep only last 100 messages
            if chat_room.message_history.len() > 100 {
                chat_room.message_history.drain(0..1);
            }

            let message = WebSocketMessage::Message {
                room,
                user,
                content,
            };

            let _ = self.broadcaster.send(message);
        } else {
            return Err("Room not found".to_string());
        }

        Ok(())
    }

    async fn get_room_history(&self, room: &str) -> Option<Vec<ChatMessage>> {
        let rooms = self.rooms.lock().ok()?;
        rooms.get(room).map(|room| room.message_history.clone())
    }

    fn get_broadcaster(&self) -> broadcast::Receiver<WebSocketMessage> {
        self.broadcaster.subscribe()
    }
}

struct WebSocketConnection {
    user: String,
    current_room: Option<String>,
    receiver: broadcast::Receiver<WebSocketMessage>,
}

impl WebSocketConnection {
    fn new(user: String, server: &ChatServer) -> Self {
        WebSocketConnection {
            user,
            current_room: None,
            receiver: server.get_broadcaster(),
        }
    }

    async fn process_message(&mut self, server: &ChatServer, message: WebSocketMessage) -> Result<(), String> {
        match message {
            WebSocketMessage::Join { room, user } => {
                if user != self.user {
                    return Err("Invalid user".to_string());
                }

                // Leave current room if any
                if let Some(current_room) = &self.current_room {
                    server.handle_leave(current_room.clone(), self.user.clone()).await?;
                }

                server.handle_join(room.clone(), user).await?;
                self.current_room = Some(room);
                Ok(())
            },
            WebSocketMessage::Leave { room, user } => {
                if user != self.user {
                    return Err("Invalid user".to_string());
                }

                server.handle_leave(room, user).await?;
                self.current_room = None;
                Ok(())
            },
            WebSocketMessage::Message { room, user, content } => {
                if user != self.user {
                    return Err("Invalid user".to_string());
                }

                if self.current_room.as_ref() != Some(&room) {
                    return Err("Not in room".to_string());
                }

                server.handle_message(room, user, content).await
            },
            _ => Err("Invalid message type from client".to_string()),
        }
    }

    async fn listen_for_broadcasts(&mut self) -> Option<WebSocketMessage> {
        match self.receiver.recv().await {
            Ok(message) => Some(message),
            Err(_) => None,
        }
    }
}

// Simulated WebSocket handler
async fn websocket_handler(server: Arc<ChatServer>, user: String) {
    let mut connection = WebSocketConnection::new(user.clone(), &server);

    // Simulate joining a room
    let join_message = WebSocketMessage::Join {
        room: "general".to_string(),
        user: user.clone(),
    };

    if let Err(e) = connection.process_message(&server, join_message).await {
        println!("Failed to join room: {}", e);
        return;
    }

    // Simulate sending a message
    let chat_message = WebSocketMessage::Message {
        room: "general".to_string(),
        user: user.clone(),
        content: "Hello, everyone!".to_string(),
    };

    if let Err(e) = connection.process_message(&server, chat_message).await {
        println!("Failed to send message: {}", e);
    }

    // Listen for broadcasts
    tokio::spawn(async move {
        while let Some(message) = connection.listen_for_broadcasts().await {
            match message {
                WebSocketMessage::Message { user: sender, content, .. } => {
                    if sender != user {
                        println!("Received message from {}: {}", sender, content);
                    }
                },
                WebSocketMessage::UserList { users, .. } => {
                    println!("Users in room: {:?}", users);
                },
                _ => {},
            }
        }
    });
}

async fn run_chat_server() {
    let server = Arc::new(ChatServer::new());

    // Simulate multiple clients
    let users = vec!["Alice", "Bob", "Charlie"];

    for user in users {
        let server_clone = Arc::clone(&server);
        let user_name = user.to_string();

        tokio::spawn(async move {
            websocket_handler(server_clone, user_name).await;
        });
    }

    // Keep server running
    tokio::time::sleep(tokio::time::Duration::from_secs(10)).await;
}

struct GameServer {
    games: Arc<Mutex<HashMap<String, Game>>>,
    broadcaster: broadcast::Sender<GameMessage>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Game {
    id: String,
    players: Vec<String>,
    state: GameState,
    created_at: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
enum GameState {
    Waiting,
    InProgress { current_player: String },
    Finished { winner: Option<String> },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
enum GameMessage {
    CreateGame { game_id: String, creator: String },
    JoinGame { game_id: String, player: String },
    StartGame { game_id: String },
    MakeMove { game_id: String, player: String, move_data: String },
    GameUpdate { game: Game },
    Error { message: String },
}

impl GameServer {
    fn new() -> Self {
        let (broadcaster, _) = broadcast::channel(1000);

        GameServer {
            games: Arc::new(Mutex::new(HashMap::new())),
            broadcaster,
        }
    }

    async fn create_game(&self, game_id: String, creator: String) -> Result<(), String> {
        let mut games = self.games.lock().map_err(|_| "Failed to lock games")?;

        if games.contains_key(&game_id) {
            return Err("Game already exists".to_string());
        }

        let game = Game {
            id: game_id.clone(),
            players: vec![creator.clone()],
            state: GameState::Waiting,
            created_at: std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_secs(),
        };

        games.insert(game_id, game.clone());

        let message = GameMessage::GameUpdate { game };
        let _ = self.broadcaster.send(message);

        Ok(())
    }

    async fn join_game(&self, game_id: String, player: String) -> Result<(), String> {
        let mut games = self.games.lock().map_err(|_| "Failed to lock games")?;

        let game = games.get_mut(&game_id).ok_or("Game not found")?;

        if game.players.len() >= 2 {
            return Err("Game is full".to_string());
        }

        if game.players.contains(&player) {
            return Err("Player already in game".to_string());
        }

        game.players.push(player);

        if game.players.len() == 2 {
            game.state = GameState::InProgress {
                current_player: game.players[0].clone(),
            };
        }

        let message = GameMessage::GameUpdate { game: game.clone() };
        let _ = self.broadcaster.send(message);

        Ok(())
    }

    async fn make_move(&self, game_id: String, player: String, move_data: String) -> Result<(), String> {
        let mut games = self.games.lock().map_err(|_| "Failed to lock games")?;

        let game = games.get_mut(&game_id).ok_or("Game not found")?;

        match &game.state {
            GameState::InProgress { current_player } => {
                if current_player != &player {
                    return Err("Not your turn".to_string());
                }

                // Simple turn switching logic
                let next_player = game.players.iter()
                    .find(|p| *p != &player)
                    .cloned()
                    .unwrap_or_else(|| player.clone());

                game.state = GameState::InProgress {
                    current_player: next_player,
                };

                let message = GameMessage::GameUpdate { game: game.clone() };
                let _ = self.broadcaster.send(message);

                Ok(())
            },
            _ => Err("Game is not in progress".to_string()),
        }
    }
}
""",
    )

    run_updater(rust_web_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    websocket_calls = [
        call
        for call in calls
        if "ChatServer" in str(call) or "websocket_handler" in str(call)
    ]
    assert len(websocket_calls) > 0, "WebSocket functions should be detected"


def test_json_api_serialization(
    rust_web_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test JSON API and serialization patterns."""
    test_file = rust_web_project / "json_api.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use serde::{Deserialize, Serialize, Serializer, Deserializer};
use serde_json::{json, Value};
use std::collections::HashMap;

#[derive(Debug, Serialize, Deserialize)]
struct ApiResponse<T> {
    success: bool,
    data: Option<T>,
    error: Option<String>,
    metadata: Option<HashMap<String, Value>>,
}

impl<T> ApiResponse<T> {
    fn success(data: T) -> Self {
        ApiResponse {
            success: true,
            data: Some(data),
            error: None,
            metadata: None,
        }
    }

    fn error(message: String) -> Self {
        ApiResponse {
            success: false,
            data: None,
            error: Some(message),
            metadata: None,
        }
    }

    fn with_metadata(mut self, metadata: HashMap<String, Value>) -> Self {
        self.metadata = Some(metadata);
        self
    }
}

#[derive(Debug, Serialize, Deserialize)]
struct PaginatedResponse<T> {
    data: Vec<T>,
    pagination: PaginationInfo,
}

#[derive(Debug, Serialize, Deserialize)]
struct PaginationInfo {
    page: u32,
    per_page: u32,
    total: u32,
    total_pages: u32,
    has_next: bool,
    has_prev: bool,
}

impl PaginationInfo {
    fn new(page: u32, per_page: u32, total: u32) -> Self {
        let total_pages = (total + per_page - 1) / per_page;

        PaginationInfo {
            page,
            per_page,
            total,
            total_pages,
            has_next: page < total_pages,
            has_prev: page > 1,
        }
    }
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct UserProfile {
    user_id: u32,
    full_name: String,
    email_address: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    phone_number: Option<String>,
    #[serde(default)]
    is_verified: bool,
    #[serde(with = "timestamp_format")]
    created_at: u64,
    #[serde(skip)]
    internal_notes: String,
}

mod timestamp_format {
    use serde::{Deserializer, Serializer, Serialize, Deserialize};

    pub fn serialize<S>(timestamp: &u64, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        let datetime = chrono::DateTime::from_timestamp(*timestamp as i64, 0)
            .unwrap_or_else(|| chrono::DateTime::from_timestamp(0, 0).unwrap());
        datetime.to_rfc3339().serialize(serializer)
    }

    pub fn deserialize<'de, D>(deserializer: D) -> Result<u64, D::Error>
    where
        D: Deserializer<'de>,
    {
        let s = String::deserialize(deserializer)?;
        chrono::DateTime::parse_from_rfc3339(&s)
            .map(|dt| dt.timestamp() as u64)
            .map_err(serde::de::Error::custom)
    }
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "type", content = "data")]
enum ApiEvent {
    UserCreated(UserProfile),
    UserUpdated { old: UserProfile, new: UserProfile },
    UserDeleted { id: u32 },
    SystemMaintenance { message: String, duration_hours: u32 },
}

#[derive(Debug, Serialize, Deserialize)]
struct WebhookPayload {
    event: ApiEvent,
    timestamp: u64,
    signature: String,
    #[serde(flatten)]
    metadata: HashMap<String, Value>,
}

fn serialize_user_profile(user: &UserProfile) -> Result<String, serde_json::Error> {
    serde_json::to_string(user)
}

fn serialize_user_profile_pretty(user: &UserProfile) -> Result<String, serde_json::Error> {
    serde_json::to_string_pretty(user)
}

fn deserialize_user_profile(json: &str) -> Result<UserProfile, serde_json::Error> {
    serde_json::from_str(json)
}

fn create_paginated_users_response(users: Vec<UserProfile>, page: u32, per_page: u32, total: u32) -> PaginatedResponse<UserProfile> {
    let pagination = PaginationInfo::new(page, per_page, total);

    PaginatedResponse {
        data: users,
        pagination,
    }
}

fn create_success_response_with_metadata<T: Serialize>(data: T, extra_info: HashMap<String, Value>) -> Result<String, serde_json::Error> {
    let response = ApiResponse::success(data).with_metadata(extra_info);
    serde_json::to_string(&response)
}

fn handle_json_parsing_errors(json: &str) -> ApiResponse<UserProfile> {
    match serde_json::from_str::<UserProfile>(json) {
        Ok(user) => ApiResponse::success(user),
        Err(e) => ApiResponse::error(format!("JSON parsing error: {}", e)),
    }
}

fn create_webhook_payload(event: ApiEvent) -> WebhookPayload {
    let mut metadata = HashMap::new();
    metadata.insert("version".to_string(), json!("1.0"));
    metadata.insert("source".to_string(), json!("user-service"));

    WebhookPayload {
        event,
        timestamp: std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs(),
        signature: "dummy_signature".to_string(),
        metadata,
    }
}

fn validate_webhook_payload(payload: &WebhookPayload) -> bool {
    // Simple validation logic
    !payload.signature.is_empty() && payload.timestamp > 0
}

fn transform_json_structure(input: Value) -> Result<Value, serde_json::Error> {
    match input {
        Value::Object(mut obj) => {
            // Transform snake_case to camelCase
            let mut new_obj = serde_json::Map::new();

            for (key, value) in obj.drain() {
                let camel_key = snake_to_camel(&key);
                let transformed_value = transform_json_structure(value)?;
                new_obj.insert(camel_key, transformed_value);
            }

            Ok(Value::Object(new_obj))
        },
        Value::Array(arr) => {
            let transformed: Result<Vec<Value>, _> = arr
                .into_iter()
                .map(transform_json_structure)
                .collect();
            Ok(Value::Array(transformed?))
        },
        other => Ok(other),
    }
}

fn snake_to_camel(s: &str) -> String {
    let mut result = String::new();
    let mut capitalize_next = false;

    for c in s.chars() {
        if c == '_' {
            capitalize_next = true;
        } else if capitalize_next {
            result.push(c.to_uppercase().next().unwrap_or(c));
            capitalize_next = false;
        } else {
            result.push(c);
        }
    }

    result
}

fn merge_json_objects(base: Value, overlay: Value) -> Value {
    match (base, overlay) {
        (Value::Object(mut base_map), Value::Object(overlay_map)) => {
            for (key, value) in overlay_map {
                base_map.insert(key, value);
            }
            Value::Object(base_map)
        },
        (_, overlay) => overlay,
    }
}

fn extract_specific_fields(input: Value, fields: &[&str]) -> Value {
    match input {
        Value::Object(obj) => {
            let mut result = serde_json::Map::new();

            for field in fields {
                if let Some(value) = obj.get(*field) {
                    result.insert(field.to_string(), value.clone());
                }
            }

            Value::Object(result)
        },
        other => other,
    }
}

async fn batch_process_json_requests(requests: Vec<String>) -> Vec<ApiResponse<Value>> {
    let mut responses = Vec::new();

    for request in requests {
        match serde_json::from_str::<Value>(&request) {
            Ok(value) => responses.push(ApiResponse::success(value)),
            Err(e) => responses.push(ApiResponse::error(format!("Parse error: {}", e))),
        }
    }

    responses
}
""",
    )

    run_updater(rust_web_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    json_calls = [
        call
        for call in calls
        if "serialize_user_profile" in str(call) or "ApiResponse" in str(call)
    ]
    assert len(json_calls) > 0, "JSON API functions should be detected"


def test_database_orm_patterns(
    rust_web_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test database and ORM patterns."""
    test_file = rust_web_project / "database.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::collections::HashMap;
use serde::{Deserialize, Serialize};

// Simulated database traits and implementations

trait Database {
    type Error;

    async fn connect(&self, url: &str) -> Result<Connection, Self::Error>;
}

trait Connection {
    type Error;

    async fn execute(&mut self, query: &str) -> Result<QueryResult, Self::Error>;
    async fn query(&mut self, query: &str) -> Result<Vec<Row>, Self::Error>;
    async fn transaction(&mut self) -> Result<Transaction, Self::Error>;
}

struct QueryResult {
    rows_affected: u64,
    last_insert_id: Option<u64>,
}

struct Row {
    data: HashMap<String, Value>,
}

impl Row {
    fn get<T>(&self, column: &str) -> Option<T>
    where
        T: From<Value>,
    {
        self.data.get(column).cloned().map(T::from)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
enum Value {
    Null,
    Int(i64),
    Float(f64),
    String(String),
    Bool(bool),
    Bytes(Vec<u8>),
}

impl From<Value> for i64 {
    fn from(value: Value) -> Self {
        match value {
            Value::Int(i) => i,
            _ => 0,
        }
    }
}

impl From<Value> for String {
    fn from(value: Value) -> Self {
        match value {
            Value::String(s) => s,
            _ => String::new(),
        }
    }
}

struct PostgresDatabase;
struct MySqlDatabase;
struct SqliteDatabase;

impl Database for PostgresDatabase {
    type Error = DatabaseError;

    async fn connect(&self, url: &str) -> Result<Connection, Self::Error> {
        // Simulate connection
        Ok(PostgresConnection { url: url.to_string() })
    }
}

impl Database for MySqlDatabase {
    type Error = DatabaseError;

    async fn connect(&self, url: &str) -> Result<Connection, Self::Error> {
        Ok(MySqlConnection { url: url.to_string() })
    }
}

impl Database for SqliteDatabase {
    type Error = DatabaseError;

    async fn connect(&self, url: &str) -> Result<Connection, Self::Error> {
        Ok(SqliteConnection { path: url.to_string() })
    }
}

#[derive(Debug)]
struct DatabaseError {
    message: String,
}

struct PostgresConnection {
    url: String,
}

struct MySqlConnection {
    url: String,
}

struct SqliteConnection {
    path: String,
}

struct Transaction {
    connection: Box<dyn Connection<Error = DatabaseError>>,
    committed: bool,
}

impl Transaction {
    async fn commit(mut self) -> Result<(), DatabaseError> {
        self.committed = true;
        Ok(())
    }

    async fn rollback(mut self) -> Result<(), DatabaseError> {
        self.committed = false;
        Ok(())
    }
}

// ORM-like patterns

#[derive(Debug, Serialize, Deserialize)]
struct User {
    id: Option<i64>,
    username: String,
    email: String,
    created_at: Option<u64>,
    updated_at: Option<u64>,
}

trait Model: Sized {
    fn table_name() -> &'static str;
    fn from_row(row: Row) -> Result<Self, DatabaseError>;
    fn to_values(&self) -> HashMap<String, Value>;
}

impl Model for User {
    fn table_name() -> &'static str {
        "users"
    }

    fn from_row(row: Row) -> Result<Self, DatabaseError> {
        Ok(User {
            id: row.get("id"),
            username: row.get("username").unwrap_or_default(),
            email: row.get("email").unwrap_or_default(),
            created_at: row.get("created_at"),
            updated_at: row.get("updated_at"),
        })
    }

    fn to_values(&self) -> HashMap<String, Value> {
        let mut values = HashMap::new();

        if let Some(id) = self.id {
            values.insert("id".to_string(), Value::Int(id));
        }
        values.insert("username".to_string(), Value::String(self.username.clone()));
        values.insert("email".to_string(), Value::String(self.email.clone()));

        if let Some(created_at) = self.created_at {
            values.insert("created_at".to_string(), Value::Int(created_at as i64));
        }
        if let Some(updated_at) = self.updated_at {
            values.insert("updated_at".to_string(), Value::Int(updated_at as i64));
        }

        values
    }
}

struct Repository<M: Model> {
    connection: Box<dyn Connection<Error = DatabaseError>>,
    _phantom: std::marker::PhantomData<M>,
}

impl<M: Model> Repository<M> {
    fn new(connection: Box<dyn Connection<Error = DatabaseError>>) -> Self {
        Repository {
            connection,
            _phantom: std::marker::PhantomData,
        }
    }

    async fn find_by_id(&mut self, id: i64) -> Result<Option<M>, DatabaseError> {
        let query = format!("SELECT * FROM {} WHERE id = {}", M::table_name(), id);
        let rows = self.connection.query(&query).await?;

        if let Some(row) = rows.into_iter().next() {
            Ok(Some(M::from_row(row)?))
        } else {
            Ok(None)
        }
    }

    async fn find_all(&mut self) -> Result<Vec<M>, DatabaseError> {
        let query = format!("SELECT * FROM {}", M::table_name());
        let rows = self.connection.query(&query).await?;

        rows.into_iter()
            .map(M::from_row)
            .collect::<Result<Vec<_>, _>>()
    }

    async fn save(&mut self, model: &M) -> Result<QueryResult, DatabaseError> {
        let values = model.to_values();
        let columns: Vec<String> = values.keys().cloned().collect();
        let placeholders: Vec<String> = (0..columns.len()).map(|i| format!("${}", i + 1)).collect();

        let query = format!(
            "INSERT INTO {} ({}) VALUES ({})",
            M::table_name(),
            columns.join(", "),
            placeholders.join(", ")
        );

        self.connection.execute(&query).await
    }

    async fn update_by_id(&mut self, id: i64, updates: HashMap<String, Value>) -> Result<QueryResult, DatabaseError> {
        let set_clauses: Vec<String> = updates.keys().enumerate()
            .map(|(i, key)| format!("{} = ${}", key, i + 2))
            .collect();

        let query = format!(
            "UPDATE {} SET {} WHERE id = $1",
            M::table_name(),
            set_clauses.join(", ")
        );

        self.connection.execute(&query).await
    }

    async fn delete_by_id(&mut self, id: i64) -> Result<QueryResult, DatabaseError> {
        let query = format!("DELETE FROM {} WHERE id = {}", M::table_name(), id);
        self.connection.execute(&query).await
    }

    async fn count(&mut self) -> Result<i64, DatabaseError> {
        let query = format!("SELECT COUNT(*) FROM {}", M::table_name());
        let rows = self.connection.query(&query).await?;

        if let Some(row) = rows.into_iter().next() {
            Ok(row.get("count").unwrap_or(0))
        } else {
            Ok(0)
        }
    }
}

struct QueryBuilder {
    table: String,
    select_fields: Vec<String>,
    where_conditions: Vec<String>,
    joins: Vec<String>,
    order_by: Vec<String>,
    limit: Option<i64>,
    offset: Option<i64>,
}

impl QueryBuilder {
    fn new(table: &str) -> Self {
        QueryBuilder {
            table: table.to_string(),
            select_fields: vec!["*".to_string()],
            where_conditions: Vec::new(),
            joins: Vec::new(),
            order_by: Vec::new(),
            limit: None,
            offset: None,
        }
    }

    fn select(mut self, fields: &[&str]) -> Self {
        self.select_fields = fields.iter().map(|s| s.to_string()).collect();
        self
    }

    fn where_eq(mut self, column: &str, value: &str) -> Self {
        self.where_conditions.push(format!("{} = '{}'", column, value));
        self
    }

    fn where_in(mut self, column: &str, values: &[&str]) -> Self {
        let value_list = values.iter()
            .map(|v| format!("'{}'", v))
            .collect::<Vec<_>>()
            .join(", ");
        self.where_conditions.push(format!("{} IN ({})", column, value_list));
        self
    }

    fn join(mut self, table: &str, on_condition: &str) -> Self {
        self.joins.push(format!("JOIN {} ON {}", table, on_condition));
        self
    }

    fn left_join(mut self, table: &str, on_condition: &str) -> Self {
        self.joins.push(format!("LEFT JOIN {} ON {}", table, on_condition));
        self
    }

    fn order_by(mut self, column: &str, direction: &str) -> Self {
        self.order_by.push(format!("{} {}", column, direction));
        self
    }

    fn limit(mut self, limit: i64) -> Self {
        self.limit = Some(limit);
        self
    }

    fn offset(mut self, offset: i64) -> Self {
        self.offset = Some(offset);
        self
    }

    fn build(self) -> String {
        let mut query = format!(
            "SELECT {} FROM {}",
            self.select_fields.join(", "),
            self.table
        );

        for join in self.joins {
            query.push_str(&format!(" {}", join));
        }

        if !self.where_conditions.is_empty() {
            query.push_str(&format!(" WHERE {}", self.where_conditions.join(" AND ")));
        }

        if !self.order_by.is_empty() {
            query.push_str(&format!(" ORDER BY {}", self.order_by.join(", ")));
        }

        if let Some(limit) = self.limit {
            query.push_str(&format!(" LIMIT {}", limit));
        }

        if let Some(offset) = self.offset {
            query.push_str(&format!(" OFFSET {}", offset));
        }

        query
    }
}

async fn connection_pooling_example() {
    struct ConnectionPool {
        connections: Vec<Box<dyn Connection<Error = DatabaseError>>>,
        max_size: usize,
    }

    impl ConnectionPool {
        fn new(max_size: usize) -> Self {
            ConnectionPool {
                connections: Vec::new(),
                max_size,
            }
        }

        async fn get_connection(&mut self) -> Option<Box<dyn Connection<Error = DatabaseError>>> {
            self.connections.pop()
        }

        fn return_connection(&mut self, connection: Box<dyn Connection<Error = DatabaseError>>) {
            if self.connections.len() < self.max_size {
                self.connections.push(connection);
            }
        }
    }

    let mut pool = ConnectionPool::new(10);

    if let Some(mut connection) = pool.get_connection().await {
        let _ = connection.query("SELECT 1").await;
        pool.return_connection(connection);
    }
}

async fn migration_example() {
    struct Migration {
        version: i32,
        name: String,
        up: String,
        down: String,
    }

    impl Migration {
        fn new(version: i32, name: &str, up: &str, down: &str) -> Self {
            Migration {
                version,
                name: name.to_string(),
                up: up.to_string(),
                down: down.to_string(),
            }
        }
    }

    struct MigrationRunner {
        connection: Box<dyn Connection<Error = DatabaseError>>,
    }

    impl MigrationRunner {
        async fn run_migration(&mut self, migration: &Migration) -> Result<(), DatabaseError> {
            println!("Running migration: {}", migration.name);
            self.connection.execute(&migration.up).await?;
            Ok(())
        }

        async fn rollback_migration(&mut self, migration: &Migration) -> Result<(), DatabaseError> {
            println!("Rolling back migration: {}", migration.name);
            self.connection.execute(&migration.down).await?;
            Ok(())
        }
    }

    let migrations = vec![
        Migration::new(
            1,
            "create_users_table",
            "CREATE TABLE users (id SERIAL PRIMARY KEY, username VARCHAR(255), email VARCHAR(255))",
            "DROP TABLE users"
        ),
        Migration::new(
            2,
            "add_timestamps_to_users",
            "ALTER TABLE users ADD COLUMN created_at TIMESTAMP, ADD COLUMN updated_at TIMESTAMP",
            "ALTER TABLE users DROP COLUMN created_at, DROP COLUMN updated_at"
        ),
    ];

    println!("Available migrations: {}", migrations.len());
}
""",
    )

    run_updater(rust_web_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    db_calls = [
        call
        for call in calls
        if "Repository" in str(call) or "QueryBuilder" in str(call)
    ]
    assert len(db_calls) > 0, "Database functions should be detected"

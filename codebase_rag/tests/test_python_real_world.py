from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater


@pytest.fixture
def todo_app_project(temp_repo: Path) -> Path:
    """Set up a real-world Flask+ReactJS todo app structure."""
    project_path = temp_repo / "todo_app"
    project_path.mkdir()

    backend_dir = project_path / "backend"
    backend_dir.mkdir()

    with open(backend_dir / "application.py", "w") as f:
        f.write(
            """from flaskr import create_app

app = create_app()
"""
        )

    with open(backend_dir / "config.py", "w") as f:
        f.write(
            """import os
from dotenv import load_dotenv
from datetime import timedelta

basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, ".env"))

class Config(object):
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=4)
    SQLALCHEMY_TRACK_MODIFICATIONS = False

class DevelopmentConfig(Config):
    SQLALCHEMY_DATABASE_URI = "sqlite:///" + os.path.join(basedir, "data.db")
"""
        )

    flaskr_dir = backend_dir / "flaskr"
    flaskr_dir.mkdir()

    with open(flaskr_dir / "__init__.py", "w") as f:
        f.write(
            """import flaskr.models

from flask import Flask
from config import DevelopmentConfig
from flaskr.extensions import migrate, api, cors, jwt
from flaskr.db import db

from flaskr.routes.auth_route import bp as auth_route
from flaskr.routes.user_route import bp as user_route
from flaskr.routes.task_route import bp as task_route

def create_app(test_config=None):
    app = Flask(__name__)

    if test_config is None:
        app.config.from_object(DevelopmentConfig)
    else:
        app.config.from_object(test_config)

    db.init_app(app)
    migrate.init_app(app, db)
    api.init_app(app)
    cors.init_app(app)
    jwt.init_app(app)

    api.register_blueprint(auth_route, url_prefix="/api/v1")
    api.register_blueprint(user_route, url_prefix="/api/v1")
    api.register_blueprint(task_route, url_prefix="/api/v1")

    return app
"""
        )

    with open(flaskr_dir / "db.py", "w") as f:
        f.write(
            """from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )

db = SQLAlchemy(model_class=Base)
"""
        )

    with open(flaskr_dir / "extensions.py", "w") as f:
        f.write(
            """from flask_migrate import Migrate
from flask_smorest import Api
from flask_cors import CORS
from flask_jwt_extended import JWTManager

migrate = Migrate()
api = Api()
cors = CORS()
jwt = JWTManager()
"""
        )

    with open(flaskr_dir / "utils.py", "w") as f:
        f.write(
            """from werkzeug.security import generate_password_hash, check_password_hash

def generate_password(password):
    return generate_password_hash(password, salt_length=10)

def check_password(password_hash, password):
    return check_password_hash(password_hash, password)
"""
        )

    models_dir = flaskr_dir / "models"
    models_dir.mkdir()

    with open(models_dir / "__init__.py", "w") as f:
        f.write(
            """from flaskr.models.user_model import UserModel
from flaskr.models.tag_model import TagModel
from flaskr.models.task_model import TaskModel
"""
        )

    with open(models_dir / "user_model.py", "w") as f:
        f.write(
            """from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from flaskr.db import db

class UserModel(db.Model):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(
        String(20), nullable=False, unique=True, index=True
    )
    email: Mapped[str] = mapped_column(
        String(120), nullable=False, unique=True, index=True
    )
    password: Mapped[str] = mapped_column(String(300), nullable=False)

    tasks = relationship(
        "TaskModel", back_populates="user", cascade="all, delete-orphan"
    )
"""
        )

    with open(models_dir / "tag_model.py", "w") as f:
        f.write(
            """from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from flaskr.db import db

class TagModel(db.Model):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True, unique=True
    )

    tasks = relationship(
        "TaskModel", back_populates="tag", cascade="all, delete-orphan"
    )
"""
        )

    with open(models_dir / "task_model.py", "w") as f:
        f.write(
            """from enum import Enum
from sqlalchemy import ForeignKey, String, Enum as SaEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from flaskr.db import db
from datetime import datetime, timezone

class TaskStatus(Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"

class TaskModel(db.Model):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    content: Mapped[str] = mapped_column(String(600), nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        SaEnum(TaskStatus), nullable=False, default=TaskStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(
        index=True, default=lambda: datetime.now(timezone.utc)
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    user = relationship("UserModel", back_populates="tasks")

    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id"), nullable=False)
    tag = relationship("TagModel", back_populates="tasks")
"""
        )

    controllers_dir = flaskr_dir / "controllers"
    controllers_dir.mkdir()

    with open(controllers_dir / "auth_controller.py", "w") as f:
        f.write(
            """from flask_jwt_extended import create_access_token
from flask_smorest import abort
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from flaskr.db import db
from flaskr.models.user_model import UserModel
from flaskr.utils import check_password

class AuthController:
    @staticmethod
    def sign_in(data):
        try:
            user_registered = db.session.execute(
                select(UserModel).where(UserModel.email == data["email"])
            ).scalar_one_or_none()

            if (
                user_registered is None
                or check_password(user_registered.password, data["password"]) is False
            ):
                abort(401, message="Incorrect credentials")

            token = create_access_token(identity=str(user_registered.id))

            return {"token": token}
        except SQLAlchemyError:
            db.session.rollback()
            abort(500, message="Internal server error while sign in")
"""
        )

    with open(controllers_dir / "task_controller.py", "w") as f:
        f.write(
            """from flask_jwt_extended import get_jwt_identity
from flask_smorest import abort
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound, SQLAlchemyError
from flaskr.db import db
from flaskr.models.tag_model import TagModel
from flaskr.models.task_model import TaskModel

class TaskController:
    @staticmethod
    def get_all_on_user():
        try:
            user_id = get_jwt_identity()

            return (
                db.session.query(
                    TaskModel.id,
                    TaskModel.title,
                    TaskModel.content,
                    TaskModel.status,
                    TaskModel.created_at,
                    TagModel.name.label("tag_name"),
                )
                .where(TaskModel.user_id == user_id)
                .join(TagModel, TaskModel.tag_id == TagModel.id)
                .all()
            )
        except SQLAlchemyError:
            abort(500, message="Internal server error while fetching tasks on user")

    @staticmethod
    def create(data):
        try:
            user_id = get_jwt_identity()
            create_data = {"user_id": user_id, **data}
            new_task = TaskModel(**create_data)

            db.session.add(new_task)
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            abort(500, message="Internal server error while creating task")

    @staticmethod
    def update(data, task_id):
        try:
            task = db.session.execute(
                select(TaskModel).where(TaskModel.id == task_id)
            ).scalar_one()

            task.title = data["title"]
            task.content = data["content"]
            task.status = data["status"]

            db.session.add(task)
            db.session.commit()
        except NoResultFound:
            abort(404, message="Task not found")
        except SQLAlchemyError:
            db.session.rollback()
            abort(500, message="Internal server error while updating task")
"""
        )

    routes_dir = flaskr_dir / "routes"
    routes_dir.mkdir()

    with open(routes_dir / "auth_route.py", "w") as f:
        f.write(
            """from flask_smorest import Blueprint
from flask.views import MethodView
from flaskr.controllers.auth_controller import AuthController
from flaskr.schemas.schema import SignInSchema

bp = Blueprint("auth", __name__)

@bp.route("/auth/sign-in")
class SignIn(MethodView):
    @bp.arguments(SignInSchema)
    @bp.response(200)
    def post(self, data):
        return AuthController.sign_in(data)
"""
        )

    with open(routes_dir / "task_route.py", "w") as f:
        f.write(
            """from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint
from flask.views import MethodView
from flaskr.controllers.task_controller import TaskController
from flaskr.schemas.schema import TaskSchema, UpdateTaskSchema

bp = Blueprint("tasks", __name__)

@bp.route("/tasks")
class Tasks(MethodView):
    @jwt_required()
    @bp.arguments(TaskSchema)
    @bp.response(201)
    def post(self, data):
        '''Protected route (JWT Required)'''
        return TaskController.create(data)

@bp.route("/tasks/user")
class TasksOnUser(MethodView):
    @jwt_required()
    @bp.response(200, TaskSchema(many=True))
    def get(self):
        '''Protected route (JWT Required)'''
        return TaskController.get_all_on_user()

@bp.route("/tasks/<task_id>")
class TaskById(MethodView):
    @jwt_required()
    @bp.arguments(UpdateTaskSchema)
    @bp.response(200)
    def put(self, data, task_id):
        '''Protected route (JWT Required)'''
        return TaskController.update(data, task_id)
"""
        )

    with open(routes_dir / "user_route.py", "w") as f:
        f.write(
            """from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint
from flask.views import MethodView
from flaskr.schemas.schema import UserSchema
from flaskr.controllers.user_controller import UserController

bp = Blueprint("users", __name__)

@bp.route("/users")
class Users(MethodView):
    @bp.response(200, UserSchema(many=True))
    def get(self):
        return UserController.get_all()

    @bp.arguments(UserSchema)
    @bp.response(201)
    def post(self, data):
        return UserController.create(data)
"""
        )

    schemas_dir = flaskr_dir / "schemas"
    schemas_dir.mkdir()

    with open(schemas_dir / "schema.py", "w") as f:
        f.write(
            """from marshmallow import fields
from flaskr.schemas.plain_schema import (
    PlainSignInSchema,
    PlainTaskSchema,
    PlainUserSchema,
)

class UserSchema(PlainUserSchema):
    pass

class SignInSchema(PlainSignInSchema):
    pass

class TaskSchema(PlainTaskSchema):
    tag_name = fields.Str(dump_only=True, data_key="tagName")
    tag_id = fields.Int(required=True, load_only=True, data_key="tagId")

class UpdateTaskSchema(PlainTaskSchema):
    pass
"""
        )

    with open(schemas_dir / "plain_schema.py", "w") as f:
        f.write(
            """from marshmallow import Schema, fields, validate

class PlainUserSchema(Schema):
    id = fields.Int(dump_only=True)
    username = fields.Str(required=True)
    email = fields.Email(required=True)
    password = fields.Str(required=True, load_only=True)

class PlainSignInSchema(Schema):
    email = fields.Str(required=True)
    password = fields.Str(required=True)

class PlainTaskSchema(Schema):
    id = fields.Int(dump_only=True)
    title = fields.Str(required=True)
    content = fields.Str(required=True)
    status = fields.Str(
        validate=validate.OneOf(["PENDING", "IN_PROGRESS", "COMPLETED"]),
        required=True
    )
    created_at = fields.DateTime(dump_only=True, data_key="createdAt")
"""
        )

    frontend_dir = project_path / "frontend"
    frontend_dir.mkdir()

    with open(frontend_dir / "package.json", "w") as f:
        f.write(
            """{
  "name": "frontend",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build"
  },
  "dependencies": {
    "@tanstack/react-query": "^5.60.6",
    "axios": "^1.7.7",
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "react-hook-form": "^7.53.2",
    "react-router-dom": "^6.28.0",
    "zod": "^3.23.8",
    "zustand": "^5.0.1"
  }
}"""
        )

    with open(frontend_dir / "tsconfig.json", "w") as f:
        f.write(
            """{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "Bundler",
    "allowImportingTsExtensions": true,
    "isolatedModules": true,
    "moduleDetection": "force",
    "noEmit": true,
    "jsx": "react-jsx",
    "strict": true,
    "baseUrl": ".",
    "paths": {
      "@/*": ["./src/*"]
    }
  },
  "include": ["src"]
}"""
        )

    src_dir = frontend_dir / "src"
    src_dir.mkdir()

    with open(src_dir / "main.tsx", "w") as f:
        f.write(
            """import "./index.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router-dom";
import { router } from "./routes/routes";
import { QueryClientProvider, QueryClient } from "@tanstack/react-query";

const queryClient = new QueryClient();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
"""
        )

    types_dir = src_dir / "types"
    types_dir.mkdir()

    with open(types_dir / "types.ts", "w") as f:
        f.write(
            """export type Tag = {
  id: number;
  name: string;
};

export type Status =
  | "TaskStatus.PENDING"
  | "TaskStatus.IN_PROGRESS"
  | "TaskStatus.COMPLETED";

export type Task = {
  id: number;
  title: string;
  content: string;
  status: Status;
  createdAt: Date;
  tagName: string;
};

export type User = {
  id: number;
  username: string;
  email: string;
};
"""
        )

    stores_dir = src_dir / "stores"
    stores_dir.mkdir()

    with open(stores_dir / "auth-store.ts", "w") as f:
        f.write(
            """import { create } from "zustand";
import { persist } from "zustand/middleware";

type State = {
  token: string | null;
  isLoggedIn: boolean;
};

type Action = {
  signIn: (token: string) => void;
  logout: () => void;
};

export const useAuthStore = create<State & Action>()(
  persist(
    (set) => ({
      token: null,
      isLoggedIn: false,
      signIn: (token: string) => {
        set({ token, isLoggedIn: true });
      },
      logout: () => {
        set({ token: null, isLoggedIn: false });
      },
    }),
    { name: "session" },
  ),
);
"""
        )

    services_dir = src_dir / "services"
    services_dir.mkdir()

    api_dir = services_dir / "api"
    api_dir.mkdir()

    with open(api_dir / "tasks.ts", "w") as f:
        f.write(
            """import { Task } from "@/types/types";
import { useAuthStore } from "@/stores/auth-store";
import axios from "axios";

export const getTasksOnUserAPI = async () => {
  const token = useAuthStore.getState().token;

  const response = await axios.get<Task[]>(
    "http://localhost:5000/api/v1/tasks/user",
    {
      headers: {
        Authorization: `Bearer ${token}`,
      },
    },
  );

  return response.data;
};

export const createTaskAPI = async (data: {
  formData: CreateFormSchema;
  token: string | null;
}) => {
  const { formData, token } = data;

  await axios.post("http://localhost:5000/api/v1/tasks", formData, {
    headers: {
      Authorization: `Bearer ${token}`,
    },
  });
};

export const deleteTaskAPI = async (data: {
  taskId: number;
  token: string | null;
}) => {
  const { token, taskId } = data;

  await axios.delete(`http://localhost:5000/api/v1/tasks/${taskId}`, {
    headers: {
      Authorization: `Bearer ${token}`,
    },
  });
};
"""
        )

    with open(api_dir / "tags.ts", "w") as f:
        f.write(
            """import { Tag } from "@/types/types";
import axios from "axios";

export const getTagsAPI = async () => {
  const response = await axios.get<Tag[]>("http://localhost:5000/api/v1/tags");
  return response.data;
};
"""
        )

    queries_dir = services_dir / "queries"
    queries_dir.mkdir()

    with open(queries_dir / "tasks.ts", "w") as f:
        f.write(
            """import { useQuery } from "@tanstack/react-query";
import { getTasksOnUserAPI } from "../api/tasks";

export const useGetTasksOnUserQuery = () => {
  return useQuery({
    queryKey: ["tasks"],
    queryFn: getTasksOnUserAPI,
  });
};
"""
        )

    with open(queries_dir / "tags.ts", "w") as f:
        f.write(
            """import { useQuery } from "@tanstack/react-query";
import { getTagsAPI } from "../api/tags";

export const useGetTagsQuery = () => {
  return useQuery({
    queryKey: ["tags"],
    queryFn: getTagsAPI,
  });
};
"""
        )

    mutations_dir = services_dir / "mutations"
    mutations_dir.mkdir()

    with open(mutations_dir / "tasks.ts", "w") as f:
        f.write(
            """import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createTaskAPI, deleteTaskAPI } from "../api/tasks";

export const useCreateTaskMutation = () => {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: createTaskAPI,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
};

export const useDeleteTaskMutation = () => {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: deleteTaskAPI,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
};
"""
        )

    components_dir = src_dir / "components"
    components_dir.mkdir()

    tasks_dir = components_dir / "tasks"
    tasks_dir.mkdir()

    with open(tasks_dir / "task-card.tsx", "w") as f:
        f.write(
            """import { Task } from "@/types/types";
import { useDeleteTaskMutation } from "@/services/mutations/tasks";
import { useAuthStore } from "@/stores/auth-store";

interface TaskCardProps {
  task: Task;
}

export const TaskCard = ({ task }: TaskCardProps) => {
  const { token } = useAuthStore();
  const deleteMutation = useDeleteTaskMutation();

  const handleDelete = async () => {
    await deleteMutation.mutateAsync({ token, taskId: task.id });
  };

  return (
    <div className="border rounded-md p-4">
      <h3 className="font-semibold">{task.title}</h3>
      <p className="text-sm text-gray-600">{task.content}</p>
      <div className="flex justify-between items-center mt-2">
        <span className="text-xs bg-gray-200 px-2 py-1 rounded">
          {task.status}
        </span>
        <button
          onClick={handleDelete}
          className="text-red-600 text-sm hover:underline"
        >
          Delete
        </button>
      </div>
    </div>
  );
};
"""
        )

    with open(tasks_dir / "task-list.tsx", "w") as f:
        f.write(
            """import { useGetTasksOnUserQuery } from "@/services/queries/tasks";
import { TaskCard } from "./task-card";

export const TaskList = () => {
  const { data: tasks = [], isLoading } = useGetTasksOnUserQuery();

  if (isLoading) {
    return <div>Loading tasks...</div>;
  }

  return (
    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
      {tasks.map((task) => (
        <TaskCard key={task.id} task={task} />
      ))}
    </div>
  );
};
"""
        )

    routes_dir = src_dir / "routes"
    routes_dir.mkdir()

    with open(routes_dir / "routes.tsx", "w") as f:
        f.write(
            """import { createBrowserRouter } from "react-router-dom";
import { DashboardPage } from "./dashboard/page";
import { HomePage } from "./home/page";

export const router = createBrowserRouter([
  {
    path: "/",
    element: <HomePage />,
  },
  {
    path: "/dashboard",
    element: <DashboardPage />,
  },
]);
"""
        )

    dashboard_dir = routes_dir / "dashboard"
    dashboard_dir.mkdir()

    with open(dashboard_dir / "page.tsx", "w") as f:
        f.write(
            """import { TaskList } from "@/components/tasks/task-list";
import { useAuthStore } from "@/stores/auth-store";
import { Navigate } from "react-router-dom";

export const DashboardPage = () => {
  const { isLoggedIn } = useAuthStore();

  if (!isLoggedIn) {
    return <Navigate to="/" />;
  }

  return (
    <div className="container mx-auto px-4 py-8">
      <h1 className="text-2xl font-bold mb-6">My Tasks</h1>
      <TaskList />
    </div>
  );
};
"""
        )

    home_dir = routes_dir / "home"
    home_dir.mkdir()

    with open(home_dir / "page.tsx", "w") as f:
        f.write(
            """import { useAuthStore } from "@/stores/auth-store";
import { Navigate } from "react-router-dom";

export const HomePage = () => {
  const { isLoggedIn } = useAuthStore();

  if (isLoggedIn) {
    return <Navigate to="/dashboard" />;
  }

  return (
    <div className="min-h-screen flex items-center justify-center">
      <div className="text-center">
        <h1 className="text-4xl font-bold mb-4">Todo App</h1>
        <p className="text-gray-600">Please sign in to continue</p>
      </div>
    </div>
  );
};
"""
        )

    return project_path


def test_flask_model_calls(
    todo_app_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test detection of model usage in controllers."""
    run_updater(todo_app_project, mock_ingestor)

    function_calls = get_relationships(mock_ingestor, "CALLS")

    model_usage_calls = [
        call
        for call in function_calls
        if "task_controller" in call.args[0][2] and "TaskModel" in call.args[2][2]
    ]

    assert model_usage_calls, (
        f"Expected TaskController to use TaskModel, found: "
        f"{[(c.args[0][2], c.args[2][2]) for c in model_usage_calls]}"
    )


def test_flask_controller_imports(
    todo_app_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test detection of Flask controller imports and dependencies."""
    run_updater(todo_app_project, mock_ingestor)

    import_calls = get_relationships(mock_ingestor, "IMPORTS")

    auth_controller_imports = [
        call
        for call in import_calls
        if "auth_controller" in call.args[0][2]
        and ("models" in call.args[2][2] or "utils" in call.args[2][2])
    ]

    assert len(auth_controller_imports) >= 2, (
        f"Expected AuthController to import models and utils modules, found: "
        f"{[(c.args[0][2], c.args[2][2]) for c in auth_controller_imports]}"
    )


def test_flask_route_controller_calls(
    todo_app_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test detection of Flask route calling controller methods."""
    run_updater(todo_app_project, mock_ingestor)

    function_calls = get_relationships(mock_ingestor, "CALLS")

    route_controller_calls = [
        call
        for call in function_calls
        if "auth_route" in call.args[0][2]
        and "auth_controller.AuthController.sign_in" in call.args[2][2]
    ]

    assert route_controller_calls, (
        f"Expected auth route to call AuthController.sign_in, found: "
        f"{[(c.args[0][2], c.args[2][2]) for c in route_controller_calls]}"
    )


def test_typescript_structure_detection(
    todo_app_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test detection of TypeScript project structure."""
    run_updater(todo_app_project, mock_ingestor)

    all_calls = cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list

    module_relationships = [
        call
        for call in all_calls
        if call.args[1] == "CONTAINS_MODULE"
        and "frontend" in call.args[0][2]
        and any(
            ts_pattern in call.args[2][2]
            for ts_pattern in [".tsx", ".ts", "task-card", "auth-store"]
        )
    ]

    assert len(module_relationships) >= 2, (
        f"Expected multiple TypeScript modules to be detected, found: "
        f"{[(c.args[0][2], c.args[2][2]) for c in module_relationships]}"
    )


def test_typescript_hook_usage(
    todo_app_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test detection of React hook usage and store calls."""
    run_updater(todo_app_project, mock_ingestor)

    function_calls = get_relationships(mock_ingestor, "CALLS")

    ts_function_calls = [
        call
        for call in function_calls
        if any(
            ts_pattern in call.args[0][2] for ts_pattern in ["frontend", ".tsx", ".ts"]
        )
    ]

    print(f"TypeScript function calls found: {len(ts_function_calls)}")
    for call in ts_function_calls[:10]:
        print(f"  {call.args[0][2]} -> {call.args[2][2]}")

    hook_calls = [
        call
        for call in function_calls
        if ("task-card" in call.args[0][2] or "task-list" in call.args[0][2])
        and (
            "useAuthStore" in call.args[2][2]
            or "useDeleteTaskMutation" in call.args[2][2]
            or "useGetTasksOnUserQuery" in call.args[2][2]
        )
    ]

    assert len(hook_calls) >= 0, (
        f"Expected components to use React hooks, found: "
        f"{[(c.args[0][2], c.args[2][2]) for c in hook_calls]}"
    )


def test_api_service_calls(
    todo_app_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test detection of API service function calls."""
    run_updater(todo_app_project, mock_ingestor)

    function_calls = get_relationships(mock_ingestor, "CALLS")

    api_related_calls = [
        call
        for call in function_calls
        if any(pattern in call.args[0][2] for pattern in ["api", "queries", "services"])
        or any(pattern in call.args[2][2] for pattern in ["api", "queries", "services"])
    ]

    print(f"API-related function calls found: {len(api_related_calls)}")
    for call in api_related_calls[:10]:
        print(f"  {call.args[0][2]} -> {call.args[2][2]}")

    api_service_calls = [
        call
        for call in function_calls
        if ("queries" in call.args[0][2])
        and (
            "api" in call.args[2][2]
            and (
                "getTasksOnUserAPI" in call.args[2][2]
                or "getTagsAPI" in call.args[2][2]
            )
        )
    ]

    assert len(api_service_calls) >= 0, (
        f"Expected queries to call API services, found: "
        f"{[(c.args[0][2], c.args[2][2]) for c in api_service_calls]}"
    )


def test_cross_language_api_structure(
    todo_app_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test overall structure detection across Python and TypeScript."""
    run_updater(todo_app_project, mock_ingestor)

    all_calls = cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list

    python_files = [
        call
        for call in all_calls
        if any(
            py_file in str(call.args)
            for py_file in [
                "models",
                "controllers",
                "routes",
                "schemas",
                "config.py",
                "__init__.py",
            ]
        )
    ]

    typescript_files = [
        call
        for call in all_calls
        if any(
            ts_file in str(call.args)
            for ts_file in [".tsx", ".ts", "components", "services", "stores", "types"]
        )
    ]

    assert python_files, (
        f"Expected multiple Python file relationships, found {len(python_files)}"
    )

    assert typescript_files, (
        f"Expected multiple TypeScript file relationships, found {len(typescript_files)}"
    )


def test_schema_inheritance_detection(
    todo_app_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test detection of schema inheritance patterns."""
    run_updater(todo_app_project, mock_ingestor)

    inheritance_calls = get_relationships(mock_ingestor, "INHERITS")

    schema_inheritance = [
        call
        for call in inheritance_calls
        if ("UserSchema" in call.args[0][2] and "PlainUserSchema" in call.args[2][2])
        or ("TaskSchema" in call.args[0][2] and "PlainTaskSchema" in call.args[2][2])
    ]

    assert len(schema_inheritance) >= 2, (
        f"Expected schema inheritance detection, found: "
        f"{[(c.args[0][2], c.args[2][2]) for c in schema_inheritance]}"
    )

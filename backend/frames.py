"""Projective frame matrices that tie annotation coordinates to image content.

Every processed asset stores a 3x3 homography ``frame`` mapping *its own*
canvas coordinates to the coordinates of the root (originally imported) image.
Geometry operations (crop, resize, rotate, flip, shear, translate,
perspective, canvas extension) contribute one more homography; color and
filter operations leave the frame untouched because pixels do not move.

The matrices use pixel coordinates with the origin at the centre of the
top-left pixel, matching Pillow's transform conventions. All functions are
pure and deterministic so the same pipeline always yields the same frame.
"""
import math

import numpy as np

IDENTITY = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def as_matrix(value):
    array = np.asarray(value, dtype=np.float64)
    if array.shape != (3, 3):
        raise ValueError("帧矩阵必须是 3x3")
    return array


def to_json(matrix):
    return [[round(float(value), 9) for value in row] for row in as_matrix(matrix)]


def multiply(first, second):
    """Composition: apply ``second`` first, then ``first``."""
    return as_matrix(first) @ as_matrix(second)


def invert(matrix):
    inverse = np.linalg.inv(as_matrix(matrix))
    return inverse


def affine(a=1.0, b=0.0, c=0.0, d=0.0, e=1.0, f=0.0):
    """Build a forward affine matrix from x' = a*x + b*y + c etc."""
    return np.array([[a, b, c], [d, e, f], [0.0, 0.0, 1.0]], dtype=np.float64)


def translation(dx, dy):
    return affine(c=dx, f=dy)


def scale_matrix(sx, sy):
    return affine(a=sx, e=sy)


def rotation_about(cx, cy, angle_degrees):
    """Counter-clockwise rotation around (cx, cy), Pillow sign convention."""
    theta = math.radians(angle_degrees)
    cosine, sine = math.cos(theta), math.sin(theta)
    move = translation(-cx, -cy)
    rotate = np.array([[cosine, sine, 0.0], [-sine, cosine, 0.0], [0.0, 0.0, 1.0]])
    return translation(cx, cy) @ rotate @ move


def inverse_affine(coefficients):
    """Invert Pillow's AFFINE inverse-map tuple (a,b,c,d,e,f).

    Pillow consumes a mapping *output -> input*. The annotation frame needs
    the opposite direction (input -> output), which is the matrix inverse.
    """
    a, b, c, d, e, f = coefficients
    pillow_inverse = np.array([[a, b, c], [d, e, f], [0.0, 0.0, 1.0]])
    return invert(pillow_inverse)


def inverse_quad(quad, size):
    """Invert Pillow's QUAD inverse-map into a forward homography.

    ``quad`` lists four source-plane points corresponding to the output
    corners in order TL, BL, BR, TR. Fitting the output->input homography and
    inverting it gives the input->output frame used by the annotations.
    """
    return invert(quad_backward(quad, size))


def quad_backward(quad, size):
    """Pillow QUAD map as a homography: output coordinates -> source coords.

    Pillow's quad tuple lists source-plane points in the same winding as the
    output corners: upper left, upper right, lower right, lower left. It uses
    outer pixel corners (0..width). Fitting against matching output corners
    yields the exact output->source homography used by ``Image.transform``.
    """
    width, height = size
    output_corners = np.array([
        (0.0, 0.0), (float(width), 0.0), (float(width), float(height)), (0.0, float(height)),
    ], dtype=np.float64)
    source_points = np.array([(quad[0], quad[1]), (quad[2], quad[3]),
                              (quad[4], quad[5]), (quad[6], quad[7])], dtype=np.float64)
    return _fit_homography(output_corners, source_points)


def _fit_homography(src, dst):
    """Least-squares homography mapping rows of src to rows of dst."""
    rows = []
    for (x, y), (u, v) in zip(src, dst):
        rows.append([x, y, 1.0, 0.0, 0.0, 0.0, -u * x, -u * y])
        rows.append([0.0, 0.0, 0.0, x, y, 1.0, -v * x, -v * y])
    design = np.asarray(rows, dtype=np.float64)
    target = dst.reshape(-1)
    coefficients, *_ = np.linalg.lstsq(design, target, rcond=None)
    # Eight fitted coefficients (h33 fixed at 1) form a 3x3 homography.
    flat = list(coefficients.reshape(-1)) + [1.0]
    return np.asarray(flat, dtype=np.float64).reshape(3, 3)


def apply(matrix, points):
    """Project an iterable of (x, y) points; returns a list of float tuples."""
    matrix = as_matrix(matrix)
    points = np.asarray(list(points), dtype=np.float64)
    if points.size == 0:
        return []
    homogeneous = np.concatenate([points, np.ones((len(points), 1))], axis=1)
    projected = homogeneous @ matrix.T
    projected = projected[:, :2] / projected[:, 2:3]
    return [(float(x), float(y)) for x, y in projected]


def point(matrix, x, y):
    return apply(matrix, [(x, y)])[0]


def corner_correspondence(forward_matrix, width, height):
    """Map the four input canvas corners through a forward matrix."""
    corners = [(0.0, 0.0), (0.0, float(height - 1)),
               (float(width - 1), float(height - 1)), (float(width - 1), 0.0)]
    return apply(forward_matrix, corners)


def fit_from_corners(source_size, mapped_corners):
    """Homography from the input canvas corners to their mapped positions."""
    width, height = source_size
    source_corners = np.array([
        (0.0, 0.0), (0.0, height - 1.0), (width - 1.0, height - 1.0), (width - 1.0, 0.0),
    ], dtype=np.float64)
    return _fit_homography(source_corners, np.asarray(mapped_corners, dtype=np.float64))

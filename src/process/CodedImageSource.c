/*
    This file is part of the Fluggo Media Library for high-quality
    video and audio processing.

    Copyright 2010 Brian J. Crowell <brian@fluggo.com>

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
*/

#include "pyframework.h"

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "fluggo.media.process.CodedImageSource"

static PyObject *py_type_packet;

EXPORT bool
py_coded_image_take_source( PyObject *source, CodedImageSourceHolder *holder ) {
    Py_CLEAR( holder->source.obj );
    Py_CLEAR( holder->csource );
    holder->source.funcs = NULL;

    if( source == NULL || source == Py_None )
        return true;

    Py_INCREF( source );
    holder->source.obj = source;
    holder->csource = PyObject_GetAttrString( source, CODED_IMAGE_SOURCE_FUNCS );

    if( !PyCapsule_IsValid( holder->csource, CODED_IMAGE_SOURCE_FUNCS ) ) {
        Py_CLEAR( holder->source.obj );
        PyErr_SetString( PyExc_Exception, "The source didn't have an acceptable " CODED_IMAGE_SOURCE_FUNCS " attribute." );
        return false;
    }

    holder->source.funcs = (coded_image_source_funcs*)
        PyCapsule_GetPointer( holder->csource, CODED_IMAGE_SOURCE_FUNCS );

    return true;
}

static PyObject *
CodedImageSource_get_frame( PyObject *self, PyObject *args ) {
    CodedImageSourceHolder holder = { { NULL } };
    int frame;

    if( !PyArg_ParseTuple( args, "i", &frame ) )
        return NULL;

    if( !py_coded_image_take_source( self, &holder ) )
        return NULL;

    coded_image *image = holder.source.funcs->getFrame( holder.source.obj, frame, 0 );
    py_coded_image_take_source( NULL, &holder );

    if( !image )
        Py_RETURN_NONE;

    PyObject *data[CODED_IMAGE_MAX_PLANES];
    int stride[CODED_IMAGE_MAX_PLANES];
    int line_count[CODED_IMAGE_MAX_PLANES];
    int count = 0;

    for( int i = 0; i < CODED_IMAGE_MAX_PLANES; i++ ) {
        if( !image->data[i] )
            break;

        count = i + 1;

        data[i] = PyByteArray_FromStringAndSize( image->data[i], image->stride[i] * image->line_count[i] );
        stride[i] = image->stride[i];
        line_count[i] = image->line_count[i];
    }

    if( image->free_func )
        image->free_func( image );

    if( count == 0 )
        Py_RETURN_NONE;

    PyObject *result = PyList_New( count );

    for( int i = 0; i < count; i++ ) {
        PyObject *member = PyObject_CallFunction( py_type_packet, "Oii", data[i], stride[i], line_count[i] );
        PyList_SET_ITEM( result, i, member );

        Py_CLEAR( data[i] );
    }

    return result;
}

static PyMethodDef CodedImageSource_methods[] = {
    { "get_frame", (PyCFunction) CodedImageSource_get_frame, METH_VARARGS,
        "Get the specified coded image from the source." },
    { NULL }
};

static void
destroy_image( coded_image *image ) {
    for( int i = 0; i < CODED_IMAGE_MAX_PLANES; i++ )
        g_free( image->data[i] );

    g_free( image );
}

static coded_image *
CodedImageSource_get_frame_from_python( PyObject *self, int frame, int quality ) {
    PyGILState_STATE gstate;
    gstate = PyGILState_Ensure();

    PyObject *result_obj = PyObject_CallMethod( (PyObject *) self, "get_frame", "i", frame );

    PyGILState_Release( gstate );

    if( !result_obj ) {
        PyErr_Print();
        return NULL;
    }

    Py_ssize_t plane_count = PySequence_Length( result_obj );

    if( plane_count == -1 ) {
        PyErr_Print();
        return NULL;
    }

    coded_image *image = g_new0( coded_image, 1 );
    image->free_func = (GFreeFunc) destroy_image;

    for( Py_ssize_t plane = 0; plane < plane_count && plane < CODED_IMAGE_MAX_PLANES; plane++ ) {
        PyObject *plane_obj = PySequence_GetItem( result_obj, plane );

        if( !plane_obj ) {
            PyErr_Print();
            Py_CLEAR(result_obj);
            destroy_image( image );

            return NULL;
        }

        PyObject *data_obj = PyObject_GetAttrString( plane_obj, "data" );

        if( data_obj == Py_None ) {
            Py_CLEAR(data_obj);
            Py_CLEAR(plane_obj);
            continue;
        }

        PyObject *stride_obj = PyObject_GetAttrString( plane_obj, "stride" );
        PyObject *line_count_obj = PyObject_GetAttrString( plane_obj, "line_count" );
        Py_CLEAR(plane_obj);

        if( !data_obj || !stride_obj || !line_count_obj ) {
            PyErr_Print();
            Py_CLEAR(data_obj);
            Py_CLEAR(stride_obj);
            Py_CLEAR(line_count_obj);
            Py_CLEAR(result_obj);
            destroy_image( image );

            return NULL;
        }

        image->stride[plane] = PyLong_AsLong( stride_obj );
        Py_CLEAR(stride_obj);

        image->line_count[plane] = PyLong_AsLong( line_count_obj );
        Py_CLEAR(line_count_obj);

        if( PyErr_Occurred() ) {
            PyErr_Print();
            Py_CLEAR(data_obj);
            Py_CLEAR(result_obj);
            destroy_image( image );

            return NULL;
        }

        Py_buffer buffer;

        if( PyObject_GetBuffer( data_obj, &buffer, PyBUF_SIMPLE ) == -1 ) {
            PyErr_Print();
            Py_CLEAR(data_obj);
            Py_CLEAR(result_obj);
            destroy_image( image );

            return NULL;
        }

        if( buffer.len != image->stride[plane] * image->line_count[plane] ) {
            Py_CLEAR(data_obj);
            Py_CLEAR(result_obj);
            PyBuffer_Release( &buffer );
            destroy_image( image );

            g_warning(
                "Error in plane %zd: expected %zd bytes, got %zd bytes.",
                plane,
                (Py_ssize_t) image->stride[plane] * image->line_count[plane],
                buffer.len );
            return NULL;
        }

        image->data[plane] = g_memdup( buffer.buf, buffer.len );

        PyBuffer_Release( &buffer );
        Py_CLEAR(data_obj);
    }

    return image;
}

static coded_image_source_funcs source_funcs = {
    .getFrame = (coded_image_getFrameFunc) CodedImageSource_get_frame_from_python,
};

static PyObject *py_source_funcs;

static PyObject *
CodedImageSource_get_funcs( PyObject *self, void *closure ) {
    Py_INCREF(py_source_funcs);
    return py_source_funcs;
}

static PyGetSetDef CodedImageSource_getsetters[] = {
    { CODED_IMAGE_SOURCE_FUNCS, (getter) CodedImageSource_get_funcs, NULL, "Coded image source C API." },
    { NULL }
};

EXPORT PyTypeObject py_type_CodedImageSource = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "fluggo.media.process.CodedImageSource",
    .tp_basicsize = 0,
    .tp_new = PyType_GenericNew,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE,
    .tp_methods = CodedImageSource_methods,
    .tp_getset = CodedImageSource_getsetters,
};

void init_CodedImageSource( PyObject *module ) {
    PyObject *collections = PyImport_ImportModule( "collections" );

    if( collections == NULL )
        return;

    PyObject *namedtuple = PyObject_GetAttrString( collections, "namedtuple" );
    Py_CLEAR( collections );

    py_type_packet = PyObject_CallFunction( namedtuple, "ss", "CodedImage", "data stride line_count" );

    Py_CLEAR( namedtuple );

    if( PyType_Ready( &py_type_CodedImageSource ) < 0 )
        return;

    Py_INCREF( (PyObject*) &py_type_CodedImageSource );
    PyModule_AddObject( module, "CodedImageSource", (PyObject *) &py_type_CodedImageSource );
    PyModule_AddObject( module, "CodedImage", py_type_packet );

    py_source_funcs = PyCapsule_New( &source_funcs,
        CODED_IMAGE_SOURCE_FUNCS, NULL );
}


